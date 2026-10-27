"""Callbacks, owner alerts, call-safety caps, retention, self-serve numbers."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db import BusinessSQL, CallLogSQL, UserSQL
from scripts.config import settings
from services import email as email_service

NUMBER = "+917900000002"


async def drain():
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture
async def owner(client, session):
    from scripts.assign_number import assign

    email = f"{uuid.uuid4().hex[:8]}@shop.in"
    r = await client.post("/api/auth/register", json={
        "email": email, "password": "Str0ngPass!2026", "full_name": "Anil", "business_name": "Anil Motors"})
    data = r.json()
    await assign(session, data["user"]["business_id"], NUMBER)
    return {"auth": {"Authorization": f"Bearer {data['access_token']}"}, "email": email,
            "business_id": data["user"]["business_id"]}


def call(sid, **extra):
    return {"CallSid": sid, "From": "+919812345678", "To": NUMBER, **extra}


# ---------------------------------------------------------------- callbacks

async def test_callback_request_alerts_owner_once_and_lists_it(client, owner):
    sid = "CA" + uuid.uuid4().hex
    before = len(email_service.outbox)
    await client.post("/api/voice/respond", data=call(sid, SpeechResult="let me speak to a human"))
    await client.post("/api/voice/respond", data=call(sid, SpeechResult="I said a human please"))
    await drain()

    alerts = [m for m in email_service.outbox[before:] if m["Subject"].startswith("Callback needed")]
    assert len(alerts) == 1 and alerts[0]["To"] == owner["email"]
    assert "+919812345678" in alerts[0].get_content()

    cbs = (await client.get("/api/dashboard/callbacks", headers=owner["auth"])).json()
    assert len(cbs) == 1 and cbs[0]["customer_phone"] == "+919812345678" and cbs[0]["reason"]
    stats = (await client.get("/api/dashboard/stats", headers=owner["auth"])).json()
    assert stats["open_callbacks"] == 1

    r = await client.post(f"/api/dashboard/calls/{cbs[0]['id']}/follow-up", headers=owner["auth"], json={"done": True})
    assert r.status_code == 200
    assert (await client.get("/api/dashboard/callbacks", headers=owner["auth"])).json() == []


async def test_unanswered_transfer_becomes_a_callback(client, owner, session):
    sid = "CA" + uuid.uuid4().hex
    await client.post("/api/voice/incoming", data=call(sid))
    r = await client.post("/api/voice/dial-status", data=call(sid, DialCallStatus="no-answer"))
    assert "call you back" in r.text and "<Hangup/>" in r.text
    row = (await session.execute(select(CallLogSQL).where(CallLogSQL.call_sid == sid))).scalar_one()
    assert row.escalation_requested and row.outcome == "escalated"

    answered = await client.post("/api/voice/dial-status", data=call("CA" + uuid.uuid4().hex, DialCallStatus="completed"))
    assert answered.text.endswith("<Hangup/></Response>")


async def test_demo_business_never_sends_alerts(client):
    before = len(email_service.outbox)
    await client.post("/api/chat", json={"message": "I want to talk to a human"})
    await drain()
    assert not [m for m in email_service.outbox[before:] if m["Subject"].startswith("Callback")]


# ------------------------------------------------------------- call safety

async def test_silent_line_hangs_up_after_three_prompts(client, owner):
    sid = "CA" + uuid.uuid4().hex
    texts = [(await client.post("/api/voice/respond", data=call(sid, SpeechResult=""))).text for _ in range(3)]
    assert "<Gather" in texts[0] and "Are you still there" in texts[1]
    assert "<Hangup/>" in texts[2] and "<Gather" not in texts[2]


async def test_speaking_resets_the_silence_count(client, owner):
    sid = "CA" + uuid.uuid4().hex
    for said in ("", "", "hello", "", ""):
        r = await client.post("/api/voice/respond", data=call(sid, SpeechResult=said))
    assert "<Gather" in r.text   # only two silences since the caller spoke


async def test_withheld_caller_id_is_not_stored_as_a_number(client, owner, session):
    sid = "CA" + uuid.uuid4().hex
    await client.post("/api/voice/incoming", data=call(sid, From="+266696687"))
    row = (await session.execute(select(CallLogSQL).where(CallLogSQL.call_sid == sid))).scalar_one()
    assert row.customer_phone is None


async def test_suspended_business_is_not_in_service(client, owner, session):
    b = (await session.execute(select(BusinessSQL).where(BusinessSQL.id == owner["business_id"]))).scalar_one()
    b.status = "suspended"
    await session.commit()
    r = await client.post("/api/voice/incoming", data=call("CA" + uuid.uuid4().hex))
    assert "not in service" in r.text


async def test_very_long_call_hands_off(client, owner, monkeypatch):
    from api.routes import voice
    monkeypatch.setattr(voice, "MAX_CALLER_TURNS", 2)
    sid = "CA" + uuid.uuid4().hex
    for said in ("hello", "what are your hours"):
        await client.post("/api/voice/respond", data=call(sid, SpeechResult=said))
    r = await client.post("/api/voice/respond", data=call(sid, SpeechResult="and another thing"))
    assert "<Hangup/>" in r.text and "call you back" in r.text


# --------------------------------------------------------------- retention

async def test_retention_scrubs_old_transcripts_only(client, owner, session):
    from services.retention import purge_expired

    await client.put("/api/business", headers=owner["auth"], json={"retention_days": 30})
    for sid in ("old", "new"):
        await client.post("/api/voice/respond", data=call(f"CA{sid}{uuid.uuid4().hex[:8]}", SpeechResult="hello"))
    rows = (await session.execute(select(CallLogSQL).where(CallLogSQL.business_id == owner["business_id"]))).scalars().all()
    rows[0].start_time = datetime.now(timezone.utc) - timedelta(days=45)
    await session.commit()

    assert await purge_expired(session) == 1
    assert await purge_expired(session) == 0          # idempotent
    await session.refresh(rows[0])
    await session.refresh(rows[1])
    assert rows[0].transcript is None and rows[0].customer_phone is None
    assert rows[0].outcome                            # stats survive
    assert rows[1].transcript is not None


# ------------------------------------------------------- self-serve numbers

class FakeProvisioning:
    def __init__(self):
        self.released = []

    async def search(self, contains=None, limit=10):
        from services.provisioning import AvailableNumber
        return [AvailableNumber("+918000000001", "080-0000-0001", "Bengaluru")]

    async def purchase(self, session, business_id, business_name, e164):
        from db import PhoneNumberSQL
        row = PhoneNumberSQL(id=uuid.uuid4().hex, business_id=business_id, e164=e164,
                             provider="twilio", provider_sid="PN" + uuid.uuid4().hex, is_active=True)
        session.add(row)
        await session.commit()
        return row

    async def release_at_provider(self, sid):
        self.released.append(sid)


@pytest.fixture
def fake_twilio(monkeypatch):
    from services import provisioning
    fake = FakeProvisioning()
    monkeypatch.setattr(provisioning, "enabled", lambda: True)
    for name in ("search", "purchase", "release_at_provider"):
        monkeypatch.setattr(provisioning, name, getattr(fake, name))
    return fake


async def test_numbers_disabled_by_default(client, owner):
    cfg = (await client.get("/api/numbers/config", headers=owner["auth"])).json()
    assert cfg["enabled"] is False
    r = await client.post("/api/numbers", headers=owner["auth"], json={"phone_number": "+918000000001"})
    assert r.status_code == 403


async def test_buying_requires_verified_email_and_respects_the_limit(client, owner, session, fake_twilio):
    auth = owner["auth"]
    r = await client.post("/api/numbers", headers=auth, json={"phone_number": "+918000000001"})
    assert r.status_code == 403 and "Verify" in r.json()["detail"]

    user = (await session.execute(select(UserSQL).where(UserSQL.email == owner["email"]))).scalar_one()
    user.is_verified = True
    await session.commit()

    # The fixture already assigned one number, and the default limit is 1.
    r = await client.post("/api/numbers", headers=auth, json={"phone_number": "+918000000001"})
    assert r.status_code == 403 and "plan includes 1" in r.json()["detail"]

    nums = (await client.get("/api/business", headers=auth)).json()["phone_numbers"]
    assert (await client.delete(f"/api/numbers/{nums[0]['id']}", headers=auth)).status_code == 204

    found = (await client.get("/api/numbers/available", headers=auth)).json()
    r = await client.post("/api/numbers", headers=auth, json={"phone_number": found[0]["e164"]})
    assert r.status_code == 201 and r.json()["e164"] == "+918000000001"

    # The released number no longer routes.
    gone = await client.post("/api/voice/incoming", data=call("CA" + uuid.uuid4().hex))
    assert "Anil Motors" not in gone.text


async def test_one_owner_cannot_release_anothers_number(client, owner, fake_twilio):
    r = await client.post("/api/auth/register", json={
        "email": "other@x.in", "password": "Str0ngPass!2026", "full_name": "Other"})
    other = {"Authorization": f"Bearer {r.json()['access_token']}"}
    nums = (await client.get("/api/business", headers=owner["auth"])).json()["phone_numbers"]
    assert (await client.delete(f"/api/numbers/{nums[0]['id']}", headers=other)).status_code == 404
    assert fake_twilio.released == []


# ------------------------------------------------------------ booking alert

async def test_booking_emails_the_owner(client, owner, monkeypatch):
    import json
    from types import SimpleNamespace as NS
    from zoneinfo import ZoneInfo
    from llm_agent import llm_provider

    await client.post("/api/business/services", headers=owner["auth"],
                      json={"name": "Car service", "duration_minutes": 60, "price": 250000})
    d = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=2)
    while d.weekday() == 6:
        d += timedelta(days=1)

    script = [
        NS(choices=[NS(message=NS(content=None, tool_calls=[NS(id="c1", type="function", function=NS(
            name="book_appointment", arguments=json.dumps({
                "service_name": "Car service", "date": d.isoformat(), "time": "11:00",
                "customer_name": "Ravi"})))]))]),
        NS(choices=[NS(message=NS(content="Booked!", tool_calls=None))]),
    ]

    async def create(**kw):
        return script.pop(0)

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm_provider, "_client", NS(chat=NS(completions=NS(create=create))))

    before = len(email_service.outbox)
    await client.post("/api/voice/respond", data=call("CA" + uuid.uuid4().hex, SpeechResult="book a service at 11"))
    await drain()
    alerts = [m for m in email_service.outbox[before:] if m["Subject"].startswith("New booking")]
    assert len(alerts) == 1 and "Ravi" in alerts[0].get_content()
    assert "+919812345678" in alerts[0].get_content()   # caller ID used as the contact
