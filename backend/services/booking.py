"""Availability and booking against a business's real calendar.

This replaces an action that returned a fabricated booking id and a
hard-coded date while telling the caller "Appointment booked successfully!".
The rule now is simple: nothing is ever confirmed to a caller unless a row
was committed here first.

Model: a business has `capacity` parallel resources (chairs, rooms,
doctors); a slot is full when that many appointments run at once anywhere
inside it. An appointment occupies [start, end + buffer). Busy time on the
owner's synced Google Calendar closes a slot outright. Opening hours, holidays and
the booking window below decide which starts are offered at all.

Double-booking safety: book() takes a row lock on the business before its
overlap check, so two callers racing for the same slot serialise on Postgres.
SQLite ignores FOR UPDATE but serialises writers at the database level.

All datetimes are stored in UTC. SQLite's DateTime drops tzinfo on write, so
anything reaching a query or a column goes through _utc() first.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import select

from db import AppointmentSQL, BusinessSQL
from services.business_profile import BusinessProfile, ServiceInfo
from services.phone import normalize_phone

logger = logging.getLogger(__name__)

# Booking window. Short-notice bookings a receptionist cannot staff are worse
# than no booking; very distant ones are usually mistakes ("next June" heard
# as "this June") and pin the calendar.
MIN_NOTICE = timedelta(minutes=30)
MAX_DAYS_AHEAD = 90

ACTIVE_STATUSES = ("scheduled", "confirmed")


class BookingError(Exception):
    """A booking could not be made. `code` is stable for callers to branch on;
    `message` is safe to say aloud; `alternatives` are bookable local datetimes."""

    def __init__(self, code: str, message: str, alternatives: Optional[List[datetime]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.alternatives = alternatives or []


@dataclass
class BookedAppointment:
    id: str
    service: ServiceInfo
    start: datetime  # business-local, aware
    end: datetime
    customer_name: str
    customer_phone: Optional[str]


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; they were written as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_local(profile: BusinessProfile, day: str, hhmm: str) -> datetime:
    """"2026-09-25" + "14:30" -> aware datetime in the business's zone."""
    try:
        d = date.fromisoformat(day.strip())
    except (ValueError, AttributeError):
        raise BookingError("bad_date", "I didn't catch the date. Which day would you like?")
    try:
        hh, mm = hhmm.strip().replace(".", ":").split(":")[:2]
        t = time(int(hh), int(mm))
    except (ValueError, AttributeError):
        raise BookingError("bad_time", "I didn't catch the time. What time would suit you?")
    return datetime.combine(d, t, tzinfo=profile.tz)


def within_booking_window(start: datetime, now: datetime) -> bool:
    return now + MIN_NOTICE <= start <= now + timedelta(days=MAX_DAYS_AHEAD)


async def _busy(session, business_id: str, lo: datetime, hi: datetime) -> List[Tuple[datetime, datetime]]:
    """Active appointments overlapping [lo, hi), as aware UTC intervals."""
    rows = (await session.execute(
        select(AppointmentSQL.start_time, AppointmentSQL.end_time).where(
            AppointmentSQL.business_id == business_id,
            AppointmentSQL.status.in_(ACTIVE_STATUSES),
            AppointmentSQL.start_time < _utc(hi),
            AppointmentSQL.end_time > _utc(lo),
        )
    )).all()
    return [(_aware(s), _aware(e)) for s, e in rows]


def _peak_overlap(start: datetime, end: datetime, buffer: timedelta,
                  busy: List[Tuple[datetime, datetime]]) -> int:
    """Most appointments running at once anywhere inside [start, end).

    Not simply "how many overlap": with two chairs, 10:00-10:30 and
    10:30-11:00 both overlap a new 10:00-11:00 booking, but never at the same
    moment, so a chair is free throughout. A sweep over interval edges gives
    the true peak.
    """
    edges = []
    for b_start, b_end in busy:
        lo, hi = max(start, b_start - buffer), min(end, b_end + buffer)
        if lo < hi:
            edges.append((lo, 1))
            edges.append((hi, -1))
    peak = current = 0
    for _, delta in sorted(edges, key=lambda e: (e[0], e[1])):  # ends before starts at a tie
        current += delta
        peak = max(peak, current)
    return peak


def _conflicts(start: datetime, end: datetime, buffer: timedelta,
               busy: List[Tuple[datetime, datetime]], capacity: int = 1,
               blocked: Optional[List[Tuple[datetime, datetime]]] = None) -> bool:
    """Full at this time? `blocked` (the owner's own calendar) closes the slot
    outright; `busy` appointments only fill it once capacity is reached."""
    if blocked and any(start < b_end and b_start < end for b_start, b_end in blocked):
        return True
    return _peak_overlap(start, end, buffer, busy) >= max(1, capacity)


async def _external_blocks(session, profile: BusinessProfile, lo: datetime, hi: datetime):
    """Busy time from the owner's synced calendar. A calendar outage must not
    stop bookings, so failures count as free (and are logged)."""
    if not profile.gcal_connected:
        return []
    try:
        from services import gcal
        return await gcal.busy(session, profile, lo, hi)
    except Exception as e:
        logger.warning("Calendar busy lookup failed for %s: %s", profile.id, e)
        return []


async def free_slots(
    session,
    profile: BusinessProfile,
    service: ServiceInfo,
    day: date,
    now: Optional[datetime] = None,
    limit: int = 6,
) -> List[datetime]:
    """Bookable start times on `day`, earliest first."""
    window = profile.hours_on(day)
    if not window:
        return []
    opens, closes = window
    now = now or profile.now()
    duration = timedelta(minutes=service.duration_minutes)
    buffer = timedelta(minutes=service.buffer_minutes)
    step = timedelta(minutes=max(5, profile.slot_minutes))

    busy = await _busy(session, profile.id, opens - buffer, closes + buffer)
    blocked = await _external_blocks(session, profile, opens, closes)

    out, t = [], opens
    while t + duration <= closes and len(out) < limit:
        if within_booking_window(t, now) and not _conflicts(t, t + duration, buffer, busy,
                                                           profile.capacity, blocked):
            out.append(t)
        t += step
    return out


async def next_available(
    session, profile: BusinessProfile, service: ServiceInfo,
    after: date, limit: int = 3, horizon_days: int = 14, now: Optional[datetime] = None,
) -> List[datetime]:
    """First few open slots on or after `after`, spanning days if needed."""
    found: List[datetime] = []
    for i in range(horizon_days):
        found += await free_slots(session, profile, service, after + timedelta(days=i),
                                  now=now, limit=limit - len(found))
        if len(found) >= limit:
            break
    return found


async def book(
    session,
    profile: BusinessProfile,
    service: ServiceInfo,
    start: datetime,
    customer_name: str,
    customer_phone: Optional[str],
    source: str = "phone",
    call_log_id: Optional[str] = None,
    notes: Optional[str] = None,
    now: Optional[datetime] = None,
) -> BookedAppointment:
    """Validate, lock, re-check and insert. Raises BookingError; commits on success."""
    if not profile.booking_enabled:
        raise BookingError("disabled", "I'm not able to book appointments directly, "
                                       "but I can take a message for the team.")
    name = (customer_name or "").strip()
    if not name:
        raise BookingError("missing_name", "Could I take a name for the booking?")

    start = start.astimezone(profile.tz)
    end = start + timedelta(minutes=service.duration_minutes)
    now = now or profile.now()

    async def alternatives():
        return await next_available(session, profile, service, max(start.date(), now.date()), now=now)

    if start < now + MIN_NOTICE:
        raise BookingError("too_soon", "That time is too soon for us to book.", await alternatives())
    if start > now + timedelta(days=MAX_DAYS_AHEAD):
        raise BookingError("too_far",
                           f"We can only book up to {MAX_DAYS_AHEAD} days ahead.")

    window = profile.hours_on(start.date())
    if not window:
        raise BookingError("closed", "We're closed that day.", await alternatives())
    if start < window[0] or end > window[1]:
        raise BookingError("outside_hours", "That's outside our opening hours.", await alternatives())

    # Serialise concurrent bookings for this business, then re-check under the
    # lock - the slot may have gone while the caller was talking.
    await session.execute(
        select(BusinessSQL.id).where(BusinessSQL.id == profile.id).with_for_update()
    )
    buffer = timedelta(minutes=service.buffer_minutes)
    busy = await _busy(session, profile.id, start - buffer, end + buffer)
    blocked = await _external_blocks(session, profile, start, end)
    if _conflicts(start, end, buffer, busy, profile.capacity, blocked):
        await session.rollback()
        raise BookingError("taken", "Sorry, that slot has just been taken.", await alternatives())

    appt = AppointmentSQL(
        id=uuid.uuid4().hex,
        business_id=profile.id,
        service_id=service.id,
        customer_name=name,
        customer_phone=normalize_phone(customer_phone) or "",
        start_time=_utc(start),
        end_time=_utc(end),
        status="scheduled",
        notes=notes,
        source=source,
        call_log_id=call_log_id,
    )
    session.add(appt)
    await session.commit()
    logger.info("Booked %s for business %s at %s", appt.id, profile.id, start.isoformat())
    _mirror_create(profile, appt.id)
    return BookedAppointment(appt.id, service, start, end, name, appt.customer_phone or None)


async def upcoming_for_phone(session, profile: BusinessProfile, phone: Optional[str],
                             now: Optional[datetime] = None) -> List[AppointmentSQL]:
    """A caller's own future appointments. Scoped by phone so one caller can
    never read or change another caller's booking."""
    key = normalize_phone(phone)
    if not key:
        return []
    now = now or profile.now()
    return list((await session.execute(
        select(AppointmentSQL).where(
            AppointmentSQL.business_id == profile.id,
            AppointmentSQL.customer_phone == key,
            AppointmentSQL.status.in_(ACTIVE_STATUSES),
            AppointmentSQL.start_time >= _utc(now),
        ).order_by(AppointmentSQL.start_time)
    )).scalars().all())


async def _owned(session, profile: BusinessProfile, appointment_id: str, phone: Optional[str]) -> AppointmentSQL:
    key = normalize_phone(phone)
    appt = (await session.execute(
        select(AppointmentSQL).where(
            AppointmentSQL.id == appointment_id,
            AppointmentSQL.business_id == profile.id,
        )
    )).scalar_one_or_none()
    # Same answer whether it is missing or someone else's.
    if not appt or not key or appt.customer_phone != key or appt.status not in ACTIVE_STATUSES:
        raise BookingError("not_found", "I couldn't find that appointment under this phone number.")
    return appt


async def cancel(session, profile: BusinessProfile, appointment_id: str, phone: Optional[str],
                 reason: Optional[str] = None) -> AppointmentSQL:
    appt = await _owned(session, profile, appointment_id, phone)
    appt.status = "cancelled"
    if reason:
        appt.notes = ((appt.notes or "") + f"\nCancelled: {reason}").strip()
    await session.commit()
    _mirror_delete(profile, appt.external_event_id)
    return appt


async def reschedule(session, profile: BusinessProfile, appointment_id: str, phone: Optional[str],
                     new_start: datetime, now: Optional[datetime] = None) -> BookedAppointment:
    """Book the new slot first, then cancel the old one - a failed reschedule
    must never leave the caller with no appointment at all."""
    old = await _owned(session, profile, appointment_id, phone)
    service = next((s for s in profile.services if s.id == old.service_id), None)
    if service is None:
        raise BookingError("not_found", "I couldn't find the service for that appointment.")
    old_id, name, old_phone, old_event = old.id, old.customer_name, old.customer_phone, old.external_event_id

    # Free the old slot for the overlap check only if the new one overlaps it
    # (moving 10:00 to 10:30 must not conflict with itself).
    old.status = "cancelled"
    await session.flush()
    try:
        booked = await book(session, profile, service, new_start, name, old_phone,
                            source="phone", notes=f"Rescheduled from {old_id}", now=now)
    except BookingError:
        await session.rollback()  # restores the original appointment
        raise
    _mirror_delete(profile, old_event)
    return booked


def _mirror_create(profile: BusinessProfile, appointment_id: str) -> None:
    """Background copy to the owner's Google Calendar; never blocks a call."""
    try:
        from services import gcal
        gcal.queue_create(profile, appointment_id)
    except Exception as e:
        logger.warning("Calendar mirror skipped: %s", e)


def _mirror_delete(profile: BusinessProfile, event_id: Optional[str]) -> None:
    try:
        from services import gcal
        gcal.queue_delete(profile, event_id)
    except Exception as e:
        logger.warning("Calendar mirror skipped: %s", e)
