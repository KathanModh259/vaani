"""Account lifecycle: sessions, password reset, verification, data rights."""

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db import CallLogSQL, UserSQL
from services import email as email_service

PW = "Str0ngPass!2026"


async def signup(client, email=None):
    email = email or f"{uuid.uuid4().hex[:8]}@clinic.in"
    r = await client.post("/api/auth/register", json={
        "email": email, "password": PW, "full_name": "Dr Iyer", "business_name": "Iyer Clinic"})
    assert r.status_code == 201, r.text
    return email, r.json()


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


async def drain():
    """Let fire-and-forget email tasks run."""
    for _ in range(3):
        await asyncio.sleep(0)


def last_link(path):
    body = email_service.outbox[-1].get_content()
    m = re.search(re.escape(path) + r"\?token=([\w.-]+)", body)
    assert m, body
    return m.group(1)


async def test_refresh_token_issues_a_new_session(client):
    _, tokens = await signup(client)
    r = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200 and r.json()["access_token"]
    # An access token is not a refresh token, and vice versa.
    assert (await client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(tokens["refresh_token"]))).status_code == 401


async def test_signup_sends_a_verification_link_that_works(client, session):
    email, tokens = await signup(client)
    await drain()
    token = last_link("/api/auth/verify-email")
    assert email_service.outbox[-1]["To"] == email

    r = await client.get(f"/api/auth/verify-email?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard?verified=1"
    me = (await client.get("/api/auth/me", headers=bearer(tokens["access_token"]))).json()
    assert me["is_verified"] is True

    # A verification token is not a session.
    assert (await client.get("/api/auth/me", headers=bearer(token))).status_code == 401


async def test_forgot_password_does_not_reveal_accounts(client):
    before = len(email_service.outbox)
    r = await client.post("/api/auth/forgot-password", json={"email": "nobody@nowhere.in"})
    await drain()
    assert r.status_code == 202 and len(email_service.outbox) == before


async def test_reset_link_works_once_and_replaces_the_password(client):
    email, _ = await signup(client)
    await client.post("/api/auth/forgot-password", json={"email": email.upper()})
    await drain()
    token = last_link("/reset-password")

    r = await client.post("/api/auth/reset-password", json={"token": token, "password": "BrandNew#2026"})
    assert r.status_code == 200 and r.json()["user"]["is_verified"] is True

    assert (await client.post("/api/auth/login", json={"email": email, "password": PW})).status_code == 401
    assert (await client.post("/api/auth/login", json={"email": email, "password": "BrandNew#2026"})).status_code == 200
    # Single use: the password changed, so the fingerprint no longer matches.
    again = await client.post("/api/auth/reset-password", json={"token": token, "password": "Another#2026"})
    assert again.status_code == 400


async def test_password_change_revokes_older_sessions(client, session):
    email, tokens = await signup(client)
    old = tokens["access_token"]

    bad = await client.post("/api/auth/change-password", headers=bearer(old),
                            json={"current_password": "wrong", "new_password": "Changed#2026"})
    assert bad.status_code == 400
    r = await client.post("/api/auth/change-password", headers=bearer(old),
                          json={"current_password": PW, "new_password": "Changed#2026"})
    assert r.status_code == 200
    new = r.json()["access_token"]

    # Tokens carry second-resolution iat, so pin the change a moment later
    # than the old token was issued to make revocation observable.
    user = (await session.execute(select(UserSQL).where(UserSQL.email == email))).scalar_one()
    user.password_changed_at = datetime.now(timezone.utc) + timedelta(seconds=2)
    await session.commit()
    assert (await client.get("/api/auth/me", headers=bearer(old))).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code == 401
    del new


async def test_export_contains_readable_transcripts(client):
    _, tokens = await signup(client)
    auth = bearer(tokens["access_token"])
    await client.post("/api/chat", headers=auth, json={"message": "hello"})

    r = await client.get("/api/account/export", headers=auth)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert "password_hash" not in data["user"]
    assert data["business"]["name"] == "Iyer Clinic"
    assert data["calls"][0]["transcript"][0]["text"] == "hello"   # decrypted
    assert len(data["hours"]) == 7


async def test_delete_account_removes_everything(client, session):
    email, tokens = await signup(client)
    auth = bearer(tokens["access_token"])
    await client.post("/api/chat", headers=auth, json={"message": "hello"})
    bid = tokens["user"]["business_id"]

    assert (await client.request("DELETE", "/api/account", headers=auth,
                                 json={"password": PW, "confirm": "yes"})).status_code == 400
    assert (await client.request("DELETE", "/api/account", headers=auth,
                                 json={"password": "nope", "confirm": "DELETE"})).status_code == 400
    r = await client.request("DELETE", "/api/account", headers=auth, json={"password": PW, "confirm": "DELETE"})
    assert r.status_code == 204

    assert (await client.post("/api/auth/login", json={"email": email, "password": PW})).status_code == 401
    left = (await session.execute(select(CallLogSQL).where(CallLogSQL.business_id == bid))).scalars().all()
    assert left == []


async def test_plans_seed_only_when_empty(session):
    from scripts.seed_plans import PLANS, upsert_plans
    from db import PlanSQL

    assert (await upsert_plans(session, only_if_empty=True))[0] == len(PLANS)
    plan = (await session.execute(select(PlanSQL).where(PlanSQL.id == "starter"))).scalar_one()
    plan.price_monthly = 1   # an operator's edit
    await session.commit()
    assert await upsert_plans(session, only_if_empty=True) == (0, 0)
    await session.refresh(plan)
    assert plan.price_monthly == 1


@pytest.mark.parametrize("path,limited", [
    ("/api/voice/respond", False), ("/health", False), ("/", False),
    ("/api/auth/login", True), ("/api/chat", True), ("/api/business", True),
])
def test_rate_limit_rules(path, limited):
    from api.core.middleware import _rule_for
    assert (_rule_for(path) is not None) == limited


def test_login_rule_is_tighter_than_default():
    from api.core.middleware import _rule_for
    assert _rule_for("/api/auth/login")[1] < _rule_for("/api/business")[1]


async def test_memory_limiter_blocks_then_recovers(monkeypatch):
    from api.core import middleware
    lim = middleware._MemoryLimiter()
    t = [1000.0]
    monkeypatch.setattr(middleware.time, "monotonic", lambda: t[0])
    assert all([(await lim.hit("k", 3, 60))[0] for _ in range(3)])
    assert (await lim.hit("k", 3, 60))[0] is False
    t[0] += 61
    assert (await lim.hit("k", 3, 60))[0] is True
