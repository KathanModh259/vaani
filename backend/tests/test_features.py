"""Capacity, reminders, messaging channels, calendar sync, forwarding, test
calls, minute caps, LLM fallback, language following, website import, relay."""

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from db import AppointmentSQL, BusinessSQL, CallLogSQL, ServiceSQL
from scripts.config import settings
from services import booking
from services.booking import BookingError
from services.business_profile import load_profile, seed_default_hours

IST = ZoneInfo("Asia/Kolkata")
MONDAY = date(2026, 10, 5)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=IST)


def at(d, hh, mm=0):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


async def make_business(session, **fields):
    bid = uuid.uuid4().hex
    session.add(BusinessSQL(id=bid, name="Glow Salon", phone="+917000000000", email="owner@glow.in", **fields))
    await session.flush()
    seed_default_hours(session, bid)
    session.add(ServiceSQL(id=f"cut-{bid[:6]}", business_id=bid, name="Haircut", duration_minutes=30,
                           price=40000, buffer_minutes=0))
    await session.commit()
    return await load_profile(session, bid)


# ------------------------------------------------------------------ capacity

async def test_capacity_allows_parallel_bookings_up_to_the_limit(session):
    p = await make_business(session, capacity=2)
    s = p.services[0]
    await booking.book(session, p, s, at(MONDAY, 10), "A", "9000000001", now=NOW)
    await booking.book(session, p, s, at(MONDAY, 10), "B", "9000000002", now=NOW)   # second chair
    with pytest.raises(BookingError) as e:
        await booking.book(session, p, s, at(MONDAY, 10), "C", "9000000003", now=NOW)
    assert e.value.code == "taken"


def test_peak_overlap_counts_simultaneous_not_total():
    """10:00-10:30 and 10:30-11:00 both overlap 10:00-11:00 but never at once."""
    from services.booking import _peak_overlap
    busy = [(at(MONDAY, 10), at(MONDAY, 10, 30)), (at(MONDAY, 10, 30), at(MONDAY, 11))]
    assert _peak_overlap(at(MONDAY, 10), at(MONDAY, 11), timedelta(0), busy) == 1
    busy.append((at(MONDAY, 10, 15), at(MONDAY, 10, 45)))
    assert _peak_overlap(at(MONDAY, 10), at(MONDAY, 11), timedelta(0), busy) == 2


async def test_owner_calendar_busy_time_closes_slots(session, monkeypatch):
    from services import gcal

    p = await make_business(session)
    p.gcal_connected = True

    async def busy(session, profile, lo, hi):
        return [(at(MONDAY, 12), at(MONDAY, 13))]

    monkeypatch.setattr(gcal, "busy", busy)
    slots = await booking.free_slots(session, p, p.services[0], MONDAY, now=NOW, limit=100)
    assert at(MONDAY, 12) not in slots and at(MONDAY, 12, 30) not in slots and at(MONDAY, 13) in slots


async def test_calendar_outage_does_not_block_booking(session, monkeypatch):
    from services import gcal

    p = await make_business(session)
    p.gcal_connected = True

    async def broken(*a, **k):
        raise RuntimeError("google is down")

    monkeypatch.setattr(gcal, "busy", broken)
    booked = await booking.book(session, p, p.services[0], at(MONDAY, 12), "A", "9000000001", now=NOW)
    assert booked.id


async def test_booking_and_cancel_are_mirrored_to_the_calendar(session, monkeypatch):
    from services import gcal

    p = await make_business(session)
    p.gcal_connected = True
    created, deleted = [], []
    monkeypatch.setattr(gcal, "busy", lambda *a, **k: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(gcal, "queue_create", lambda profile, appt_id: created.append(appt_id))
    monkeypatch.setattr(gcal, "queue_delete", lambda profile, event_id: deleted.append(event_id))

    b = await booking.book(session, p, p.services[0], at(MONDAY, 10), "A", "9000000001", now=NOW)
    assert created == [b.id]
    row = (await session.execute(select(AppointmentSQL).where(AppointmentSQL.id == b.id))).scalar_one()
    row.external_event_id = "evt123"
    await session.commit()
    await booking.cancel(session, p, b.id, "9000000001")
    assert deleted == ["evt123"]


# ------------------------------------------------------------------ reminders

async def test_reminder_sent_once_about_a_day_ahead(session, monkeypatch):
    from services import notify, reminders

    p = await make_business(session, reminders_enabled=True)
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=23)
    session.add(AppointmentSQL(id="r1", business_id=p.id, service_id=p.services[0].id, customer_name="A",
                               customer_phone="+919000000001", start_time=start,
                               end_time=start + timedelta(minutes=30), status="scheduled"))
    session.add(AppointmentSQL(id="r2", business_id=p.id, service_id=p.services[0].id, customer_name="B",
                               customer_phone="+919000000002", start_time=now + timedelta(days=3),
                               end_time=now + timedelta(days=3, minutes=30), status="scheduled"))
    await session.commit()

    sent = []

    async def fake_send(session, profile, appt_id, to, service_name, start):
        sent.append((appt_id, to, service_name))
        return True

    monkeypatch.setattr(notify, "send_reminder", fake_send)
    assert await reminders.send_due(session, now) == 1
    assert await reminders.send_due(session, now) == 0            # claimed; never twice
    assert sent == [("r1", "+919000000001", "Haircut")]


async def test_no_reminders_when_business_has_them_off(session, monkeypatch):
    from services import reminders

    p = await make_business(session, reminders_enabled=False)
    now = datetime.now(timezone.utc)
    session.add(AppointmentSQL(id="r3", business_id=p.id, service_id=p.services[0].id, customer_name="A",
                               customer_phone="+919000000001", start_time=now + timedelta(hours=23),
                               end_time=now + timedelta(hours=23, minutes=30), status="scheduled"))
    await session.commit()
    assert await reminders.send_due(session, now) == 0


async def test_whatsapp_channel_uses_approved_template(session, monkeypatch):
    from services import notify

    p = await make_business(session, message_channel="whatsapp")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setattr(settings, "TWILIO_WHATSAPP_FROM", "+14155238886")
    monkeypatch.setattr(settings, "WHATSAPP_REMINDER_CONTENT_SID", "HX123")
    calls = []
    monkeypatch.setattr(notify, "_send_blocking", lambda **kw: calls.append(kw) or "SM1")

    assert await notify.send_reminder(session, p, "a1", "+919000000001", "Haircut", at(MONDAY, 10))
    assert calls[0]["whatsapp"] is True and calls[0]["content_sid"] == "HX123"
    assert calls[0]["variables"]["1"] == "Glow Salon" and calls[0]["variables"]["2"] == "Haircut"


async def test_whatsapp_without_template_falls_back_to_sms(session, monkeypatch):
    from services import notify

    p = await make_business(session, message_channel="whatsapp")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", "+917000000099")
    calls = []
    monkeypatch.setattr(notify, "_send_blocking", lambda **kw: calls.append(kw) or "SM1")
    assert await notify.send_reminder(session, p, "a1", "+919000000001", "Haircut", at(MONDAY, 10))
    assert not calls[0].get("whatsapp") and "reminder of your Haircut" in calls[0]["body"]


# --------------------------------------------------------- forwarding / test

async def test_forwarded_call_routes_by_the_clients_own_number(client):
    r = await client.post("/api/auth/register", json={
        "email": "fwd@x.in", "password": "Str0ngPass!2026", "full_name": "F", "business_name": "Forward Dental"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    await client.put("/api/business", headers=auth, json={"forward_from": "079 2654 1234"})
    r = await client.post("/api/voice/incoming", data={
        "CallSid": "CA" + uuid.uuid4().hex, "From": "+919812345678",
        "To": "+917999000000", "ForwardedFrom": "+917926541234"})
    assert "Forward Dental" in r.text


async def test_outbound_test_call_uses_the_signed_business_param(client):
    r = await client.post("/api/auth/register", json={
        "email": "tc@x.in", "password": "Str0ngPass!2026", "full_name": "T", "business_name": "Test Call Clinic"})
    bid = r.json()["user"]["business_id"]
    params = {"CallSid": "CA" + uuid.uuid4().hex, "From": "+917000000001", "To": "+919876543210",
              "Direction": "outbound-api"}
    r = await client.post(f"/api/voice/incoming?bid={bid}", data=params)
    assert "Test Call Clinic" in r.text and f"/api/voice/respond?bid={bid}" in r.text

    # An inbound call cannot pick a business via the query string.
    params["Direction"] = "inbound"
    r = await client.post(f"/api/voice/incoming?bid={bid}", data=params)
    assert "Test Call Clinic" not in r.text


async def test_test_call_requires_configuration(client):
    r = await client.post("/api/auth/register", json={
        "email": "tc2@x.in", "password": "Str0ngPass!2026", "full_name": "T"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.post("/api/business/test-call", headers=auth, json={})).status_code == 503


# ------------------------------------------------------------- usage / caps

async def _owner_with_calls(session, minutes, cap):
    p = await make_business(session, monthly_minute_cap=cap)
    session.add(CallLogSQL(id=uuid.uuid4().hex, business_id=p.id, call_sid="CA" + uuid.uuid4().hex,
                           start_time=datetime.now(timezone.utc), duration_seconds=minutes * 60, outcome="completed_faq"))
    session.add(CallLogSQL(id=uuid.uuid4().hex, business_id=p.id, call_sid="web_" + uuid.uuid4().hex[:12],
                           start_time=datetime.now(timezone.utc), duration_seconds=999 * 60, outcome="completed_faq"))
    await session.commit()
    return await load_profile(session, p.id)


async def test_usage_counts_phone_minutes_only(session):
    from services import usage
    p = await _owner_with_calls(session, 45, 100)
    s = await usage.summary(session, p)
    assert s["minutes_used"] == 45 and s["minutes_allowed"] == 100 and s["percent"] == 45


async def test_usage_alerts_fire_once_per_threshold(session):
    from services import usage
    p = await _owner_with_calls(session, 85, 100)
    assert await usage.check_and_alert(session, p) == 80
    assert await usage.check_and_alert(session, p) is None      # not again this month


async def test_hard_cap_takes_messages_instead_of_running_the_ai(client, session, monkeypatch):
    from scripts.assign_number import assign
    p = await _owner_with_calls(session, 120, 100)
    await assign(session, p.id, "+917900000077")
    monkeypatch.setattr(settings, "HARD_MINUTE_CAP", True)
    r = await client.post("/api/voice/incoming", data={
        "CallSid": "CA" + uuid.uuid4().hex, "From": "+919812345678", "To": "+917900000077"})
    assert "will call you back" in r.text and "<Hangup/>" in r.text and "<Gather" not in r.text


# ------------------------------------------------------------- LLM fallback

async def test_fallback_llm_answers_when_primary_fails(monkeypatch):
    from llm_agent import llm_provider

    async def broken(**kw):
        raise ConnectionError("primary down")

    async def works(**kw):
        return NS(choices=[NS(message=NS(content="from fallback", tool_calls=None))])

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm_provider, "_client", NS(chat=NS(completions=NS(create=broken))))
    monkeypatch.setattr(llm_provider, "_fallback", NS(chat=NS(completions=NS(create=works))))
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "backup-model")
    reply = await llm_provider.run_agent([{"role": "user", "content": "hi"}], [],
                                         dispatch=None, booking_made=lambda: False)
    assert reply == "from fallback"


# ---------------------------------------------------------------- language

def test_script_language_detection():
    from services.language import script_language
    assert script_language("आपका अपॉइंटमेंट बुक हो गया है") == "hi-IN"
    assert script_language("તમારી એપોઇન્ટમેન્ટ બુક થઈ ગઈ છે") == "gu-IN"
    assert script_language("You're booked for Monday") is None
    assert script_language("Haan ji, booking ho gayi") is None      # romanised Hinglish


def test_phone_voice_follows_the_reply_script():
    from api.routes.voice import _gather, _say
    assert 'language="hi-IN"' in _say("नमस्ते, कैसे मदद करूँ?", "en-IN")
    g = _gather("નમસ્તે, હું કેવી રીતે મદદ કરી શકું?", "en-IN")
    assert g.count('language="gu-IN"') == 2          # recogniser and voice both follow


# ------------------------------------------------------------ website import

@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("127.0.0.1", False), ("10.0.0.5", False), ("169.254.169.254", False),
    ("192.168.1.1", False), ("::1", False), ("fd00::1", False),
])
def test_ssrf_guard_only_allows_public_addresses(ip, public):
    from services.site_import import _public
    assert _public(ip) is public


async def test_resolver_refuses_private_hosts():
    from services.site_import import PublicOnlyResolver
    with pytest.raises(OSError):
        await PublicOnlyResolver().resolve("localhost", 80)


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://127.0.0.1/", "http://[::1]/", "http://10.1.2.3/admin",
])
async def test_import_refuses_private_ip_literals_before_connecting(url, monkeypatch):
    """aiohttp skips DNS for IP literals, so the resolver guard alone would
    let these through. They must be refused before any connection is made."""
    import aiohttp
    from services.site_import import ImportError_, fetch_text

    def no_network(*a, **k):
        raise AssertionError("tried to connect")

    monkeypatch.setattr(aiohttp.ClientSession, "get", no_network)
    with pytest.raises(ImportError_) as e:
        await fetch_text(url)
    assert "not a public" in e.value.message


def test_html_to_text_drops_scripts():
    from services.site_import import html_to_text
    text = html_to_text("<html><head><title>x</title></head><body><script>evil()</script>"
                        "<h1>Glow Salon</h1><p>Open Mon-Sat 10am-8pm</p></body></html>")
    assert "Glow Salon" in text and "evil" not in text


def test_imported_draft_is_sanitised():
    from services.site_import import _clean
    out = _clean({
        "hours": [{"day_of_week": 0, "is_open": True, "opens": "10:00", "closes": "20:00"},
                  {"day_of_week": 0, "is_open": True, "opens": "11:00", "closes": "12:00"},  # duplicate day
                  {"day_of_week": 9, "is_open": True, "opens": "10:00", "closes": "20:00"},  # bad day
                  {"day_of_week": 1, "is_open": True, "opens": "21:00", "closes": "09:00"}], # inverted
        "services": [{"name": "Haircut", "duration_minutes": 30, "price_rupees": 400},
                     {"name": "", "price_rupees": 1}, {"name": "Facial", "price_rupees": "cheap"}],
        "faqs": [{"question": "Parking?", "answer": "Yes"}, {"question": "x"}],
    })
    assert [h["day_of_week"] for h in out["hours"]] == [0]
    assert out["services"][0]["price"] == 40000 and out["services"][1]["price"] is None
    assert len(out["services"]) == 2 and len(out["faqs"]) == 1


async def test_import_endpoint_needs_the_ai_model(client):
    r = await client.post("/api/auth/register", json={
        "email": "imp@x.in", "password": "Str0ngPass!2026", "full_name": "I"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/api/business/import", headers=auth, json={"url": "example.com"})
    assert r.status_code == 503


# ----------------------------------------------------------------- relay

async def test_relay_turn_speaks_reply_and_ends_on_goodbye(session, db_url):
    import db
    from api.routes.relay import RelaySession

    p = await make_business(session)
    manager = await db.get_manager()
    relay = RelaySession(manager.get_session)
    out = []

    async def send(m):
        out.append(m)

    sid = "CA" + uuid.uuid4().hex
    await relay.handle({"type": "setup", "callSid": sid, "from": "+919812345678",
                        "customParameters": {"business_id": p.id}}, send)
    await relay.handle({"type": "prompt", "voicePrompt": "what are your hours", "last": True}, send)
    assert out[-1]["type"] == "text" and "Monday" in out[-1]["token"]

    await relay.handle({"type": "prompt", "voicePrompt": "thanks, bye", "last": True}, send)
    assert out[-1]["type"] == "end" and json.loads(out[-1]["handoffData"])["action"] == "hangup"
    row = (await session.execute(select(CallLogSQL).where(CallLogSQL.call_sid == sid))).scalar_one()
    assert row.business_id == p.id


async def test_relay_sends_filler_while_a_slow_turn_runs(session, db_url, monkeypatch):
    import db
    from api.routes import relay as relay_mod
    from llm_agent.agent import TurnResult

    p = await make_business(session)

    async def slow(*a, **k):
        await asyncio.sleep(0.2)
        return TurnResult(reply="Done.", source="rules", outcome="completed_faq")

    monkeypatch.setattr(relay_mod, "FILLER_AFTER", 0.05)
    monkeypatch.setattr("services.call_session.run_turn", slow)
    r = relay_mod.RelaySession((await db.get_manager()).get_session)
    out = []

    async def send(m):
        out.append(m)

    await r.handle({"type": "setup", "callSid": "CA1", "customParameters": {"business_id": p.id}}, send)
    await r.handle({"type": "prompt", "voicePrompt": "book me in", "last": True}, send)
    assert [m["token"] for m in out if m["type"] == "text"] == ["One moment.", "Done."]


async def test_relay_twiml_in_relay_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_MODE", "relay")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://vaani.example.com")
    r = await client.post("/api/voice/incoming", data={
        "CallSid": "CA" + uuid.uuid4().hex, "From": "+919812345678", "To": "+917999999999"})
    assert "<ConversationRelay" in r.text and 'url="wss://vaani.example.com/api/voice/relay"' in r.text


async def test_relay_ended_transfers_or_recovers(client):
    r = await client.post("/api/voice/relay-ended", data={
        "CallSid": "CA1", "From": "+919812345678", "To": "+917999999999",
        "HandoffData": json.dumps({"action": "transfer", "to": "+919000000009"})})
    assert "<Dial" in r.text and "+919000000009" in r.text
    r = await client.post("/api/voice/relay-ended", data={
        "CallSid": "CA2", "From": "+919812345678", "To": "+917999999999", "CallStatus": "in-progress"})
    assert "<Gather" in r.text      # socket dropped: keep the caller, fall back
