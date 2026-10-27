"""Availability and booking against a real (migrated) database.

The clock is pinned with `now=` so these do not depend on today's date.
Reference week: Monday 2026-10-05 .. Sunday 2026-10-11, Asia/Kolkata.
"""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from db import BusinessHolidaysSQL, BusinessSQL, ServiceSQL
from services import booking
from services.booking import BookingError
from services.business_profile import load_profile, seed_default_hours

IST = ZoneInfo("Asia/Kolkata")
MONDAY = date(2026, 10, 5)
SUNDAY = date(2026, 10, 11)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=IST)  # the Thursday before


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


@pytest.fixture
async def clinic(session):
    """Mon-Sat 09:00-18:00, one 30-min and one 45-min service."""
    bid = uuid.uuid4().hex
    session.add(BusinessSQL(id=bid, name="Test Clinic", phone="", email="t@t.in"))
    await session.flush()
    seed_default_hours(session, bid)
    session.add(ServiceSQL(id="checkup", business_id=bid, name="Check-up", duration_minutes=30, price=50000, buffer_minutes=0))
    session.add(ServiceSQL(id="cleaning", business_id=bid, name="Teeth cleaning", duration_minutes=45, price=120000, buffer_minutes=0))
    await session.commit()
    return await load_profile(session, bid)


def svc(profile, sid):
    return next(s for s in profile.services if s.id == sid)


async def test_slots_follow_opening_hours(session, clinic):
    slots = await booking.free_slots(session, clinic, svc(clinic, "checkup"), MONDAY, now=NOW, limit=100)
    assert slots[0] == at(MONDAY, 9)
    assert slots[-1] == at(MONDAY, 17, 30)          # last start that ends by 18:00
    assert all(s.minute in (0, 30) for s in slots)   # 30-minute grid
    assert await booking.free_slots(session, clinic, svc(clinic, "checkup"), SUNDAY, now=NOW) == []


async def test_booked_slot_disappears_and_cannot_be_double_booked(session, clinic):
    s = svc(clinic, "checkup")
    await booking.book(session, clinic, s, at(MONDAY, 10), "Asha", "9876543210", now=NOW)

    slots = await booking.free_slots(session, clinic, s, MONDAY, now=NOW, limit=100)
    assert at(MONDAY, 10) not in slots

    with pytest.raises(BookingError) as e:
        await booking.book(session, clinic, s, at(MONDAY, 10), "Ravi", "9123456780", now=NOW)
    assert e.value.code == "taken"
    assert e.value.alternatives and at(MONDAY, 10) not in e.value.alternatives


async def test_long_service_blocks_overlapping_starts(session, clinic):
    await booking.book(session, clinic, svc(clinic, "cleaning"), at(MONDAY, 10), "Asha", "9876543210", now=NOW)
    slots = await booking.free_slots(session, clinic, svc(clinic, "checkup"), MONDAY, now=NOW, limit=100)
    assert at(MONDAY, 10, 30) not in slots   # 10:00-10:45 overlaps 10:30
    assert at(MONDAY, 11) in slots


@pytest.mark.parametrize("start,code", [
    (at(MONDAY, 8), "outside_hours"),
    (at(MONDAY, 17, 45), "outside_hours"),        # would end 18:15
    (at(SUNDAY, 11), "closed"),
    (NOW + timedelta(minutes=10), "too_soon"),
    (NOW + timedelta(days=120), "too_far"),
])
async def test_invalid_times_are_refused(session, clinic, start, code):
    with pytest.raises(BookingError) as e:
        await booking.book(session, clinic, svc(clinic, "checkup"), start, "Asha", "9876543210", now=NOW)
    assert e.value.code == code


async def test_holidays_close_the_day(session, clinic):
    session.add(BusinessHolidaysSQL(id="h1", business_id=clinic.id, date=MONDAY, reason="Diwali"))
    await session.commit()
    profile = await load_profile(session, clinic.id)
    assert await booking.free_slots(session, profile, svc(profile, "checkup"), MONDAY, now=NOW) == []
    with pytest.raises(BookingError) as e:
        await booking.book(session, profile, svc(profile, "checkup"), at(MONDAY, 10), "A", "9876543210", now=NOW)
    assert e.value.code == "closed"


async def test_cancel_is_scoped_to_the_callers_phone(session, clinic):
    b = await booking.book(session, clinic, svc(clinic, "checkup"), at(MONDAY, 10), "Asha", "+91 98765 43210", now=NOW)

    with pytest.raises(BookingError):
        await booking.cancel(session, clinic, b.id, "9123456780")      # someone else

    # Same number, written differently, is the same caller.
    await booking.cancel(session, clinic, b.id, "098765 43210")
    assert at(MONDAY, 10) in await booking.free_slots(session, clinic, svc(clinic, "checkup"), MONDAY, now=NOW, limit=100)


async def test_reschedule_moves_the_booking(session, clinic):
    s = svc(clinic, "checkup")
    old = await booking.book(session, clinic, s, at(MONDAY, 10), "Asha", "9876543210", now=NOW)
    new = await booking.reschedule(session, clinic, old.id, "9876543210", at(MONDAY, 15), now=NOW)

    assert new.start == at(MONDAY, 15)
    upcoming = await booking.upcoming_for_phone(session, clinic, "9876543210", now=NOW)
    assert [a.id for a in upcoming] == [new.id]


async def test_failed_reschedule_keeps_the_original(session, clinic):
    s = svc(clinic, "checkup")
    mine = await booking.book(session, clinic, s, at(MONDAY, 10), "Asha", "9876543210", now=NOW)
    await booking.book(session, clinic, s, at(MONDAY, 15), "Ravi", "9123456780", now=NOW)

    with pytest.raises(BookingError):
        await booking.reschedule(session, clinic, mine.id, "9876543210", at(MONDAY, 15), now=NOW)

    upcoming = await booking.upcoming_for_phone(session, clinic, "9876543210", now=NOW)
    assert [a.id for a in upcoming] == [mine.id]   # never left with nothing


async def test_booking_disabled_is_refused(session, clinic):
    clinic.booking_enabled = False
    with pytest.raises(BookingError) as e:
        await booking.book(session, clinic, svc(clinic, "checkup"), at(MONDAY, 10), "A", "9876543210", now=NOW)
    assert e.value.code == "disabled"


@pytest.mark.skipif(not __import__("os").environ.get("TEST_DATABASE_URL"),
                    reason="needs Postgres: SQLite serialises writers, so there is no race to lose")
async def test_simultaneous_bookings_for_one_slot_cannot_both_win(session, clinic, monkeypatch):
    """Two callers, two connections, same slot, same instant. The business
    row lock must make exactly one of them win."""
    import asyncio
    import db

    # Widen the race window: pause between "is the slot free?" and the insert,
    # as a slow network would. Without the row lock, both callers now see the
    # slot free and both book it (verified by deleting the lock).
    real_busy = booking._busy

    async def slow_busy(*args, **kwargs):
        result = await real_busy(*args, **kwargs)
        await asyncio.sleep(0.3)
        return result

    monkeypatch.setattr(booking, "_busy", slow_busy)
    manager = await db.get_manager()
    s1, s2 = await manager.get_session(), await manager.get_session()
    service = svc(clinic, "checkup")

    async def attempt(s, name):
        try:
            await booking.book(s, clinic, service, at(MONDAY, 11), name, "98765" + name[-5:].zfill(5), now=NOW)
            return "won"
        except BookingError as e:
            return e.code

    try:
        results = await asyncio.gather(attempt(s1, "Caller 00001"), attempt(s2, "Caller 00002"))
    finally:
        await s1.close()
        await s2.close()
    assert sorted(results) == ["taken", "won"]


def test_phone_normalisation():
    from services.phone import normalize_phone, same_number

    for variant in ("+91 98765 43210", "9876543210", "098765-43210", "919876543210", "0091 98765 43210"):
        assert normalize_phone(variant) == "+919876543210", variant
    assert not same_number("9876543210", "9876543211")
    assert normalize_phone("") is None and normalize_phone("abc") is None


async def test_profile_rendering(clinic):
    assert clinic.hours_summary() == "Monday to Saturday: 9 AM to 6 PM; Sunday: closed."
    greeting = clinic.spoken_greeting()
    assert "Test Clinic" in greeting and "AI assistant" in greeting
    assert clinic.find_service("cleaning").id == "cleaning"
    assert clinic.find_service("CHECK-UP").id == "checkup"
    assert clinic.find_service("haircut") is None
    ctx = clinic.prompt_context()
    assert "Teeth cleaning - 45 min - 1,200 rupees" in ctx


async def test_alternatives_are_near_the_requested_time_not_the_earliest(session, clinic):
    """A caller asking for 4pm should hear 3:30 or 4:30, not 9am."""
    monday = MONDAY
    checkup = svc(clinic, "checkup")
    # Busy: the morning, plus 15:00 and 16:00. Free near 16:00: 15:30, 16:30.
    for hh in (9, 10, 11, 12, 15, 16):
        await booking.book(session, clinic, checkup, at(monday, hh),
                           f"Filler {hh}", "+919800000000", now=NOW)

    with pytest.raises(BookingError) as error:
        await booking.book(session, clinic, checkup, at(monday, 16), "Late", "+919800000002", now=NOW)
    assert error.value.code == "taken"

    alternatives = error.value.alternatives
    assert alternatives, "a refused slot must bring alternatives"
    assert at(monday, 16) not in alternatives
    # Closest first: half past three or half past four, never 9am.
    assert alternatives[0] in (at(monday, 15, 30), at(monday, 16, 30))
    assert all(abs((a - at(monday, 16)).total_seconds()) <= 2 * 3600 for a in alternatives)


async def test_nearest_slots_spill_to_the_next_day_when_the_day_is_full(session, clinic):
    monday = MONDAY
    checkup = svc(clinic, "checkup")
    slots = await booking.free_slots(session, clinic, checkup, monday, now=NOW, limit=200)
    for slot in slots:
        await booking.book(session, clinic, checkup, slot, "Filler", "+919800000003", now=NOW)

    found = await booking.nearest_slots(session, clinic, checkup, at(monday, 16, 0), now=NOW)
    assert found and all(s.date() > monday for s in found)
