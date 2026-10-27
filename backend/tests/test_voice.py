"""Twilio webhooks: routing, signatures, memory across turns, TwiML shape."""

import uuid

import pytest

from scripts.config import settings

NUMBER = "+917900000001"


@pytest.fixture
async def clinic(client, session):
    """A signed-up business with a Twilio number assigned."""
    from scripts.assign_number import assign
    from sqlalchemy import select
    from db import UserSQL

    email = f"{uuid.uuid4().hex[:8]}@clinic.in"
    r = await client.post("/api/auth/register", json={
        "email": email, "password": "Str0ngPass!2026", "full_name": "Dr Rao", "business_name": "Rao Clinic"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    user = (await session.execute(select(UserSQL).where(UserSQL.email == email))).scalar_one()
    await assign(session, user.business_id, NUMBER)
    return {"auth": auth, "business_id": user.business_id}


def call(sid="CA" + "1" * 32, **extra):
    return {"CallSid": sid, "From": "+919812345678", "To": NUMBER, **extra}


async def test_incoming_call_reaches_the_dialled_business(client, clinic):
    r = await client.post("/api/voice/incoming", data=call())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "Rao Clinic" in r.text
    assert "AI assistant" in r.text                 # disclosure is always spoken
    assert "<Gather" in r.text and 'language="en-IN"' in r.text


async def test_unassigned_number_is_not_in_service_in_production(client, clinic, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    # Isolate routing: production also refuses unsigned webhooks (tested below).
    monkeypatch.setattr(settings, "TWILIO_VALIDATE_SIGNATURE", False)
    r = await client.post("/api/voice/incoming", data=call(To="+917999999999"))
    assert "not in service" in r.text and "<Hangup/>" in r.text


async def test_unassigned_number_reaches_demo_outside_production(client):
    r = await client.post("/api/voice/incoming", data=call(To="+917999999999"))
    assert "Bright Smile Dental" in r.text


async def test_call_remembers_earlier_turns(client, clinic, session):
    sid = "CA" + uuid.uuid4().hex
    await client.post("/api/voice/incoming", data=call(sid))
    await client.post("/api/voice/respond", data=call(sid, SpeechResult="hello"))
    r = await client.post("/api/voice/respond", data=call(sid, SpeechResult="what are your hours"))
    assert "Monday to Saturday" in r.text

    from services.call_logger import find_call, transcript_of
    turns = transcript_of(await find_call(session, sid))
    assert [t["speaker"] for t in turns] == ["agent", "caller", "agent", "caller", "agent"]
    assert turns[0]["text"].startswith("Thank you for calling Rao Clinic")

    stats = (await client.get("/api/dashboard/stats", headers=clinic["auth"])).json()
    assert stats["total_calls"] == 1          # filed under this client, not the demo


async def test_silence_reprompts(client, clinic):
    r = await client.post("/api/voice/respond", data=call(SpeechResult=""))
    assert "did not catch that" in r.text and "<Gather" in r.text


async def test_goodbye_hangs_up(client, clinic):
    r = await client.post("/api/voice/respond", data=call(SpeechResult="thank you, bye"))
    assert "<Hangup/>" in r.text and "<Gather" not in r.text


async def test_status_callback_records_carrier_duration(client, clinic, session):
    sid = "CA" + uuid.uuid4().hex
    await client.post("/api/voice/incoming", data=call(sid))
    r = await client.post("/api/voice/status-callback", data=call(sid, CallStatus="completed", CallDuration="95"))
    assert r.status_code == 204
    from services.call_logger import find_call
    assert (await find_call(session, sid)).duration_seconds == 95


async def test_greeting_is_spoken_in_the_business_language(client, clinic):
    await client.put("/api/business", headers=clinic["auth"], json={"language": "hi-IN", "greeting": "Namaste!"})
    r = await client.post("/api/voice/incoming", data=call())
    assert 'language="hi-IN"' in r.text and "Namaste!" in r.text


# ------------------------------------------------------------- signatures

@pytest.fixture
def signing(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "test-twilio-token")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://vaani.example.com")


def sign(path, params):
    from twilio.request_validator import RequestValidator
    return RequestValidator("test-twilio-token").compute_signature("https://vaani.example.com" + path, params)


async def test_unsigned_webhook_is_rejected(client, clinic, signing):
    r = await client.post("/api/voice/incoming", data=call())
    assert r.status_code == 403


async def test_forged_signature_is_rejected(client, clinic, signing):
    r = await client.post("/api/voice/incoming", data=call(), headers={"X-Twilio-Signature": "bm90LXJlYWw="})
    assert r.status_code == 403


async def test_correctly_signed_webhook_is_accepted(client, clinic, signing):
    params = call()
    r = await client.post("/api/voice/incoming", data=params,
                          headers={"X-Twilio-Signature": sign("/api/voice/incoming", params)})
    assert r.status_code == 200 and "Rao Clinic" in r.text


async def test_production_without_token_rejects_webhooks(client, clinic, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    r = await client.post("/api/voice/incoming", data=call())
    assert r.status_code == 403


# ------------------------------------------------------------- escalation
# These two cases pin down the transfer-vs-message policy in
# services/escalation.py (escalation_route).

async def test_transfer_dials_staff_during_open_hours(client, clinic, monkeypatch):
    from services.business_profile import BusinessProfile

    await client.put("/api/business", headers=clinic["auth"], json={"transfer_phone": "+919000000009"})
    monkeypatch.setattr(BusinessProfile, "is_open_at", lambda self, moment: True)
    r = await client.post("/api/voice/respond", data=call(SpeechResult="I want to speak to a human"))
    assert "<Dial" in r.text and "+919000000009" in r.text


async def test_after_hours_takes_a_message_even_with_a_transfer_number(client, clinic, monkeypatch):
    from services.business_profile import BusinessProfile

    await client.put("/api/business", headers=clinic["auth"], json={"transfer_phone": "+919000000009"})
    monkeypatch.setattr(BusinessProfile, "is_open_at", lambda self, moment: False)
    r = await client.post("/api/voice/respond", data=call(SpeechResult="I want to speak to a human"))
    assert "<Dial" not in r.text and "call you back" in r.text


async def test_no_transfer_number_means_take_a_message(client, clinic):
    r = await client.post("/api/voice/respond", data=call(SpeechResult="I want to speak to a human"))
    assert "<Dial" not in r.text
    assert "call you back" in r.text
