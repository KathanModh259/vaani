"""The no-model booking flow: the demo must book with zero API keys.

These run through /api/chat exactly as the browser does, so they cover the
route, the rules path, the draft stored on the call row, and the real
`appointments` table - not just the parser.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from db import AppointmentSQL, BusinessSQL, ServiceSQL
from llm_agent import booking_flow
from services.business_profile import load_profile, seed_default_hours

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
async def sample_profile(session):
    """The same shape as the demo clinic, with no shared global state."""
    bid = "flow-clinic"
    if (await session.get(BusinessSQL, bid)) is None:
        session.add(BusinessSQL(id=bid, name="Flow Clinic", phone="", email="f@f.in"))
        await session.flush()
        seed_default_hours(session, bid)
        for sid, name, minutes in (("s1", "Dental check-up", 30), ("s2", "Teeth cleaning", 45)):
            session.add(ServiceSQL(id=sid, business_id=bid, name=name, duration_minutes=minutes,
                                   price=50000, buffer_minutes=0))
        await session.commit()
    return await load_profile(session, bid)


def bookable_day(offset: int = 2) -> str:
    """A Monday-Saturday at least two days out, in the clinic's zone."""
    d = datetime.now(IST).date() + timedelta(days=offset)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d.isoformat()


def weekday_of(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%A")


async def appointments(session):
    return (await session.execute(select(AppointmentSQL))).scalars().all()


async def say(client, message, session_id=None):
    payload = {"message": message}
    if session_id:
        payload["session_id"] = session_id
    return (await client.post("/api/chat", json=payload)).json()


# ------------------------------------------------------------------ parsing

def test_days_people_actually_say():
    now = datetime(2026, 10, 26, 9, 30, tzinfo=IST)      # a Monday
    assert booking_flow.parse_day("tomorrow", now).isoformat() == "2026-10-27"
    assert booking_flow.parse_day("day after tomorrow", now).isoformat() == "2026-10-28"
    assert booking_flow.parse_day("on Friday", now).isoformat() == "2026-10-30"
    assert booking_flow.parse_day("27th October", now).isoformat() == "2026-10-27"
    assert booking_flow.parse_day("October 30", now).isoformat() == "2026-10-30"
    assert booking_flow.parse_day("27/10", now).isoformat() == "2026-10-27"
    assert booking_flow.parse_day("2026-11-03", now).isoformat() == "2026-11-03"
    assert booking_flow.parse_day("how much is a cleaning", now) is None


def test_next_month_rolls_forward_instead_of_into_the_past():
    now = datetime(2026, 10, 26, 9, 30, tzinfo=IST)
    assert booking_flow.parse_day("5th November", now).isoformat() == "2026-11-05"
    assert booking_flow.parse_day("2nd October", now).isoformat() == "2027-10-02"


def test_times_are_read_the_way_callers_say_them():
    assert booking_flow.parse_time("4pm") == (16, 0)
    assert booking_flow.parse_time("4:30 pm") == (16, 30)
    assert booking_flow.parse_time("16:30") == (16, 30)
    assert booking_flow.parse_time("at 4") == (16, 0)
    assert booking_flow.parse_time("4 baje") == (16, 0)
    assert booking_flow.parse_time("at 11") == (11, 0)
    assert booking_flow.parse_time("noon") == (12, 0)
    assert booking_flow.parse_time("10 am") == (10, 0)
    # A phone number must never be mistaken for an appointment time.
    assert booking_flow.parse_time("9988776655") is None


def test_names_and_numbers():
    assert booking_flow.parse_name("my name is Meera Patel", False) == "Meera Patel"
    assert booking_flow.parse_name("Meera", True) == "Meera"
    assert booking_flow.parse_name("meera", False) is None          # only when asked
    assert booking_flow.parse_name("I want to book a checkup", True) is None
    assert booking_flow.parse_phone("9988776655") == "+919988776655"
    assert booking_flow.parse_phone("+91 99887 76655") == "+919988776655"
    assert booking_flow.parse_phone("4pm") is None


def test_a_date_or_time_is_never_read_as_a_phone_number():
    """The stored phone is what a caller is called back on, and what scopes
    cancellation. "on 2026-10-28 at 11, 9988776655" once stored 202610291198123."""
    assert booking_flow.parse_phone(
        "teeth cleaning on 2026-10-28 at 11 am, name Meera, 9988776655"
    ) == "+919988776655"
    assert booking_flow.parse_phone("cleaning 27th October at 11, 99887 76655") == "+919988776655"
    assert booking_flow.parse_phone("11 9988776655") == "+919988776655"
    assert booking_flow.parse_phone("2026-10-28") is None
    assert booking_flow.parse_phone("at 11 am") is None
    assert booking_flow.parse_phone("no number") is None


def test_service_survives_how_callers_phrase_it(sample_profile):
    profile = sample_profile
    assert booking_flow.parse_service("I need a cleaning", profile).name == "Teeth cleaning"
    assert booking_flow.parse_service("dental checkup please", profile).name == "Dental check-up"
    assert booking_flow.parse_service("what are your hours", profile) is None


# ------------------------------------------------------------- conversation

async def test_booking_completes_with_no_model_key(client, session, monkeypatch):
    """The flagship demo: no OPENAI_API_KEY, no OpenRouter key, still books."""
    from llm_agent import llm_provider
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)

    day = bookable_day()
    r = await say(client, "I want to book an appointment")
    assert "Which one" in r["response"] and r["source"] == "rules"
    sid = r["session_id"]

    r = await say(client, f"teeth cleaning on {weekday_of(day)} at 11 am", sid)
    assert "name" in r["response"].lower()

    r = await say(client, "my name is Meera Patel", sid)
    assert "phone" in r["response"].lower()

    r = await say(client, "9988776655", sid)
    assert "booked" in r["response"].lower(), r["response"]

    rows = await appointments(session)
    assert len(rows) == 1
    assert rows[0].customer_name == "Meera Patel"
    assert rows[0].customer_phone == "+919988776655"
    assert rows[0].status == "scheduled"

    # The draft is gone, so the next booking starts clean.
    from services.call_logger import find_call
    call = await find_call(session, sid)
    assert booking_flow.DRAFT_KEY not in (call.call_metadata or {})


async def test_one_sentence_books_in_a_single_turn(client, session, monkeypatch):
    from llm_agent import llm_provider
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)

    day = bookable_day()
    r = await say(client, f"consultation on {day} at 4pm, my name is Ravi Shah, 9123456780")
    assert "booked" in r["response"].lower(), r["response"]
    rows = await appointments(session)
    assert len(rows) == 1 and rows[0].customer_name == "Ravi Shah"


async def test_second_caller_is_offered_alternatives_not_a_second_booking(client, session, monkeypatch):
    from llm_agent import llm_provider
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)

    day = bookable_day()
    first = await say(client, f"dental check-up on {day} at 10 am, name Asha, 9000000001")
    assert "booked" in first["response"].lower()

    second = await say(client, f"dental check-up on {day} at 10 am, name Bela, 9000000002")
    assert "booked" not in second["response"].lower()
    assert "offer" in second["response"].lower() or "taken" in second["response"].lower()
    assert len(await appointments(session)) == 1


async def test_the_stored_phone_survives_a_sentence_with_a_date_in_it(client, session, monkeypatch):
    from llm_agent import llm_provider
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)

    day = bookable_day()
    r = await say(client, f"dental check-up on {day} at 3 pm, name Date Test, 9812340000")
    assert "booked" in r["response"].lower(), r["response"]

    rows = await appointments(session)
    assert rows[0].customer_phone == "+919812340000"


async def test_closed_day_is_answered_with_open_days(client, session, monkeypatch):
    from llm_agent import llm_provider
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)

    # The demo clinic is closed on Sunday.
    today = datetime.now(IST).date()
    sunday = today + timedelta(days=(6 - today.weekday()) % 7)
    r = await say(client, f"cleaning on {sunday.isoformat()} at 11 am")
    assert "closed" in r["response"].lower(), r["response"]


async def test_an_faq_midway_does_not_lose_the_booking(client, session, monkeypatch):
    from llm_agent import llm_provider
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)

    sid = (await say(client, "I'd like to book a cleaning"))["session_id"]
    r = await say(client, "do you have parking?", sid)
    assert "parking" in r["response"].lower()          # answered, not "which service"
    r = await say(client, f"{weekday_of(bookable_day())} at 3 pm", sid)
    assert "name" in r["response"].lower()              # still mid-booking


async def test_three_misses_hands_off_instead_of_looping(client, session, monkeypatch):
    from llm_agent import llm_provider
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)

    sid = (await say(client, "I want to book"))["session_id"]
    replies = [(await say(client, "hmm", sid))["response"] for _ in range(3)]
    assert any("someone from the team" in r.lower() for r in replies), replies

    from services.call_logger import find_call
    call = await find_call(session, sid)
    assert call.escalation_requested is True
    assert booking_flow.DRAFT_KEY not in (call.call_metadata or {})