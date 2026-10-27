"""Sessions, password reset, verification, export and deletion."""

import asyncio
import re
import uuid

import pytest

from services import email as email_service


@pytest.fixture(autouse=True)
def clear_outbox():
    email_service.outbox.clear()
    yield
    email_service.outbox.clear()


async def flush_email():
    """Let queued (fire-and-forget) emails run."""
    await asyncio.gather(*list(email_service._background))


def link_from(msg, path):
    body = msg.get_content()
    m = re.search(re.escape(path) + r"\?token=([\w\-\.]+)", body)
    assert m, body
    return m.group(1)


async def signup(client, email=None, password="Str0ngPass!2026"):
    email = email or f"{uuid.uuid4().hex[:8]}@clinic.in"
    r = await client.post("/api/auth/register", json={
        "email": email, "password": password, "full_name": "Dr Iyer", "business_name": "Iyer Clinic"})
    assert r.status_code == 201, r.text
    return email, r.json()


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_refresh_token_issues_a_new_pair(client):
    _, t = await signup(client)
    r = await client.post("/api/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 200
    assert (await client.get("/api/auth/me", headers=bearer(r.json()))).status_code == 200


async def test_token_types_are_not_interchangeable(client):
    _, t = await signup(client)
    # A refresh token is not a session, and a session is not a refresh token.
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {t['refresh_token']}"})).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": t["access_token"]})).status_code == 401


async def test_forgot_password_does_not_reveal_accounts(client):
    r = await client.post("/api/auth/forgot-password", json={"email": "nobody@nowhere.in"})
    assert r.status_code == 202
    await flush_email()
    assert email_service.outbox == []


async def test_password_reset_end_to_end(client, monkeypatch):
    import time
    email, t = await signup(client)
    await flush_email()
    email_service.outbox.clear()

    assert (await client.post("/api/auth/forgot-password", json={"email": email.upper()})).status_code == 202
    await flush_email()
    [msg] = email_service.outbox
    assert msg["To"] == email
    token = link_from(msg, "/reset-password")

    time.sleep(1.1)  # tokens are second-resolution; make "before the reset" unambiguous
    r = await client.post("/api/auth/reset-password", json={"token": token, "password": "BrandNew!2026"})
    assert r.status_code == 200, r.text

    # The link is single-use, the old session is dead, the new password works.
    assert (await client.post("/api/auth/reset-password", json={"token": token, "password": "Another!2026"})).status_code == 400
    assert (await client.get("/api/auth/me", headers=bearer(t))).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": t["refresh_token"]})).status_code == 401
    assert (await client.post("/api/auth/login", json={"email": email, "password": "BrandNew!2026"})).status_code == 200


async def test_reset_rejects_garbage_and_weak_passwords(client):
    assert (await client.post("/api/auth/reset-password", json={"token": "nope", "password": "BrandNew!2026"})).status_code == 400
    assert (await client.post("/api/auth/reset-password", json={"token": "nope", "password": "short"})).status_code == 422


async def test_change_password_signs_out_other_sessions(client):
    import time
    email, t = await signup(client)
    assert (await client.post("/api/auth/change-password", headers=bearer(t), json={
        "current_password": "wrong-password", "new_password": "BrandNew!2026"})).status_code == 400

    time.sleep(1.1)
    r = await client.post("/api/auth/change-password", headers=bearer(t), json={
        "current_password": "Str0ngPass!2026", "new_password": "BrandNew!2026"})
    assert r.status_code == 200
    assert (await client.get("/api/auth/me", headers=bearer(t))).status_code == 401       # old session
    assert (await client.get("/api/auth/me", headers=bearer(r.json()))).status_code == 200  # this one


async def test_email_verification_link(client):
    email, t = await signup(client)
    await flush_email()
    [msg] = email_service.outbox
    token = link_from(msg, "/api/auth/verify-email")
    assert (await client.get("/api/auth/me", headers=bearer(t))).json()["is_verified"] is False

    r = await client.get(f"/api/auth/verify-email?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard?verified=1"
    assert (await client.get("/api/auth/me", headers=bearer(t))).json()["is_verified"] is True

    # A reset token is not a verification token.
    bad = await client.get("/api/auth/verify-email?token=" + t["access_token"], follow_redirects=False)
    assert bad.headers["location"] == "/dashboard?verified=0"


async def test_export_contains_everything_readable(client):
    email, t = await signup(client)
    auth = bearer(t)
    await client.post("/api/business/services", headers=auth, json={"name": "Consult", "price": 50000})
    await client.post("/api/chat", headers=auth, json={"message": "hello"})

    r = await client.get("/api/account/export", headers=auth)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["user"]["email"] == email and "password_hash" not in data["user"]
    assert data["business"]["name"] == "Iyer Clinic"
    assert [s["name"] for s in data["services"]] == ["Consult"]
    [call] = data["calls"]
    assert call["transcript"][0]["text"] == "hello"     # decrypted, not ciphertext


async def test_delete_account_removes_everything(client, session):
    from sqlalchemy import func, select
    from db import BusinessSQL, CallLogSQL

    email, t = await signup(client)
    auth = bearer(t)
    await client.post("/api/chat", headers=auth, json={"message": "hello"})
    bid = (await client.get("/api/auth/me", headers=auth)).json()["business_id"]

    assert (await client.request("DELETE", "/api/account", headers=auth,
                                 json={"password": "Str0ngPass!2026", "confirm": "yes"})).status_code == 400
    assert (await client.request("DELETE", "/api/account", headers=auth,
                                 json={"password": "wrong", "confirm": "DELETE"})).status_code == 400
    assert (await client.request("DELETE", "/api/account", headers=auth,
                                 json={"password": "Str0ngPass!2026", "confirm": "DELETE"})).status_code == 204

    assert (await client.post("/api/auth/login", json={"email": email, "password": "Str0ngPass!2026"})).status_code == 401
    assert (await session.execute(select(BusinessSQL).where(BusinessSQL.id == bid))).scalar_one_or_none() is None
    assert (await session.execute(select(func.count()).select_from(CallLogSQL)
                                  .where(CallLogSQL.business_id == bid))).scalar_one() == 0
