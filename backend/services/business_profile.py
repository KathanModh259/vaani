"""Everything the receptionist knows about one business, loaded once per turn.

The agent used to answer from strings baked into the code - every client's
callers heard the same dental clinic's hours and prices. Now the only source
of facts is the owner's own settings: this module loads them into a plain
snapshot that the LLM prompt, the rules fallback and the booking engine all
read from, so there is exactly one place a fact can come from.

Weekdays follow Python's convention throughout: Monday = 0 ... Sunday = 6.
Prices are integers in minor units (paise), matching plans and Stripe/Razorpay.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from db import BusinessHolidaysSQL, BusinessHoursSQL, BusinessSQL, FAQSQL, ServiceSQL

logger = logging.getLogger(__name__)

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# What a new business gets until the owner edits it: the common Indian
# small-business week. Better than no hours at all, which would make every
# availability check answer "closed".
DEFAULT_HOURS = {d: ("09:00", "18:00") for d in range(6)}  # Mon-Sat, Sunday closed

AI_DISCLOSURE = "I'm Vaani, an AI assistant, and this call is transcribed."


@dataclass
class ServiceInfo:
    id: str
    name: str
    description: str
    duration_minutes: int
    price: int  # minor units
    buffer_minutes: int


@dataclass
class DayHours:
    is_open: bool
    opens: Optional[time] = None
    closes: Optional[time] = None


@dataclass
class BusinessProfile:
    id: str
    name: str
    phone: str = ""
    email: str = ""          # where owner alerts go
    status: str = "active"   # active | inactive | suspended
    time_zone: str = "Asia/Kolkata"
    currency: str = "INR"
    greeting: Optional[str] = None
    about: Optional[str] = None
    transfer_phone: Optional[str] = None
    language: str = "en-IN"
    booking_enabled: bool = True
    sms_confirmations: bool = False
    slot_minutes: int = 30
    capacity: int = 1
    message_channel: str = "sms"
    reminders_enabled: bool = False
    forward_from: Optional[str] = None
    gcal_connected: bool = False
    monthly_minute_cap: Optional[int] = None
    services: List[ServiceInfo] = field(default_factory=list)
    hours: Dict[int, DayHours] = field(default_factory=dict)
    holidays: Dict[date, str] = field(default_factory=dict)
    faqs: List[Tuple[str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------ time

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.time_zone or "Asia/Kolkata")
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Unknown time zone %r for business %s; using Asia/Kolkata",
                           self.time_zone, self.id)
            return ZoneInfo("Asia/Kolkata")

    def now(self) -> datetime:
        """Current wall-clock time at the business, timezone-aware."""
        return datetime.now(self.tz)

    def hours_on(self, day: date) -> Optional[Tuple[datetime, datetime]]:
        """Opening and closing datetimes for a date, or None if closed."""
        if day in self.holidays:
            return None
        h = self.hours.get(day.weekday())
        if not h or not h.is_open or not h.opens or not h.closes:
            return None
        return (datetime.combine(day, h.opens, tzinfo=self.tz),
                datetime.combine(day, h.closes, tzinfo=self.tz))

    def is_open_at(self, moment: datetime) -> bool:
        window = self.hours_on(moment.astimezone(self.tz).date())
        return bool(window and window[0] <= moment < window[1])

    # --------------------------------------------------------------- lookups

    def find_service(self, name: Optional[str]) -> Optional[ServiceInfo]:
        """Match a service by what a caller (or the LLM) called it.

        Exact match first, then containment either way ("cleaning" finds
        "Teeth cleaning"). With a single service, any name resolves to it -
        a one-service business should never fail a booking over wording.
        """
        if not self.services:
            return None
        if len(self.services) == 1 and name is not None:
            return self.services[0]
        wanted = _norm(name)
        if not wanted:
            return None
        for s in self.services:
            if _norm(s.name) == wanted:
                return s
        for s in self.services:
            n = _norm(s.name)
            if wanted in n or n in wanted:
                return s
        return None

    # ------------------------------------------------------------ rendering

    def format_price(self, minor: int) -> str:
        """Spoken-friendly price: "1,200 rupees", not "₹1200" (TTS reads that badly)."""
        major = minor / 100
        amount = f"{major:,.0f}" if major == int(major) else f"{major:,.2f}"
        if (self.currency or "").upper() == "INR":
            return f"{amount} rupees"
        return f"{amount} {self.currency.upper()}"

    def hours_summary(self) -> str:
        """One spoken line, collapsing runs of identical days."""
        if not self.hours:
            return "Opening hours have not been set."
        parts, d = [], 0
        while d < 7:
            h = self.hours.get(d)
            key = (h.opens, h.closes) if h and h.is_open else None
            end = d
            while end + 1 < 7:
                nxt = self.hours.get(end + 1)
                nkey = (nxt.opens, nxt.closes) if nxt and nxt.is_open else None
                if nkey != key:
                    break
                end += 1
            days = DAY_NAMES[d] if d == end else f"{DAY_NAMES[d]} to {DAY_NAMES[end]}"
            parts.append(f"{days}: closed" if key is None
                         else f"{days}: {_fmt_time(key[0])} to {_fmt_time(key[1])}")
            d = end + 1
        return "; ".join(parts) + "."

    def spoken_greeting(self) -> str:
        """The first thing a caller hears. The AI disclosure is not optional:
        several jurisdictions require callers be told they are talking to an
        automated system and that the call is recorded or transcribed."""
        base = (self.greeting or "").strip() or f"Thank you for calling {self.name}."
        return f"{base} {AI_DISCLOSURE} How can I help you today?"

    def prompt_context(self) -> str:
        """The facts block for the LLM system prompt."""
        today = self.now()
        lines = [
            f"Business: {self.name}",
            f"Current local date and time: {today.strftime('%A %d %B %Y, %H:%M')} ({self.time_zone})",
            f"Opening hours: {self.hours_summary()}",
        ]
        upcoming = sorted(d for d in self.holidays if d >= today.date())[:5]
        if upcoming:
            lines.append("Closed on: " + ", ".join(
                f"{d.strftime('%a %d %b')}" + (f" ({self.holidays[d]})" if self.holidays[d] else "")
                for d in upcoming))
        if self.services:
            lines.append("Services (name - duration - price):")
            for s in self.services:
                price = self.format_price(s.price) if s.price else "price on request"
                desc = f" - {s.description}" if s.description else ""
                lines.append(f"  * {s.name} - {s.duration_minutes} min - {price}{desc}")
        else:
            lines.append("Services: none listed. Do not quote any prices.")
        if self.about:
            lines.append(f"About the business: {self.about.strip()}")
        if self.faqs:
            lines.append("Frequently asked questions (answer only from these):")
            for q, a in self.faqs:
                lines.append(f"  Q: {q.strip()}\n  A: {a.strip()}")
        lines.append("Online booking: " + ("enabled" if self.booking_enabled and self.services
                                            else "NOT available - take a message instead"))
        lines.append("Live transfer to staff: " + ("available" if self.transfer_phone
                                                    else "NOT available - offer to take a message"))
        return "\n".join(lines)


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ઀-૿ ]+", "", (s or "").lower()).strip()


def _fmt_time(t: time) -> str:
    h12 = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    return f"{h12}:{t.minute:02d} {suffix}" if t.minute else f"{h12} {suffix}"


def _parse_hhmm(s: Optional[str]) -> Optional[time]:
    if not s:
        return None
    try:
        hh, mm = s.strip().split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


async def load_profile(session, business_id: str) -> Optional[BusinessProfile]:
    business = (
        await session.execute(select(BusinessSQL).where(BusinessSQL.id == business_id))
    ).scalar_one_or_none()
    if business is None:
        return None

    services = (await session.execute(
        select(ServiceSQL).where(ServiceSQL.business_id == business_id).order_by(ServiceSQL.name)
    )).scalars().all()
    hours = (await session.execute(
        select(BusinessHoursSQL).where(BusinessHoursSQL.business_id == business_id)
    )).scalars().all()
    holidays = (await session.execute(
        select(BusinessHolidaysSQL).where(BusinessHolidaysSQL.business_id == business_id)
    )).scalars().all()
    faqs = (await session.execute(
        select(FAQSQL).where(FAQSQL.business_id == business_id).order_by(FAQSQL.created_at)
    )).scalars().all()

    return BusinessProfile(
        id=business.id,
        name=business.name,
        phone=business.phone or "",
        email=business.email or "",
        status=business.status or "active",
        time_zone=business.time_zone or "Asia/Kolkata",
        currency=business.currency or "INR",
        greeting=business.greeting,
        about=business.about,
        transfer_phone=business.transfer_phone,
        language=business.language or "en-IN",
        booking_enabled=bool(business.booking_enabled) if business.booking_enabled is not None else True,
        sms_confirmations=bool(business.sms_confirmations),
        slot_minutes=business.slot_minutes or 30,
        capacity=max(1, business.capacity or 1),
        message_channel=business.message_channel or "sms",
        reminders_enabled=bool(business.reminders_enabled),
        forward_from=business.forward_from,
        gcal_connected=bool(business.gcal_token),
        monthly_minute_cap=business.monthly_minute_cap,
        services=[
            ServiceInfo(
                id=s.id, name=s.name, description=s.description or "",
                duration_minutes=s.duration_minutes or 30, price=s.price or 0,
                buffer_minutes=s.buffer_minutes or 0,
            )
            for s in services
        ],
        hours={
            h.day_of_week: DayHours(
                is_open=bool(h.is_open),
                opens=_parse_hhmm(h.start_time),
                closes=_parse_hhmm(h.end_time),
            )
            for h in hours if h.day_of_week is not None
        },
        holidays={h.date: h.reason or "" for h in holidays if h.date},
        faqs=[(f.question, f.answer) for f in faqs],
    )


def seed_default_hours(session, business_id: str) -> None:
    """Give a brand-new business a working week. Caller commits."""
    for day in range(7):
        opens_closes = DEFAULT_HOURS.get(day)
        session.add(BusinessHoursSQL(
            id=uuid.uuid4().hex,
            business_id=business_id,
            day_of_week=day,
            start_time=opens_closes[0] if opens_closes else None,
            end_time=opens_closes[1] if opens_closes else None,
            is_open=opens_closes is not None,
        ))


def next_open_days(profile: BusinessProfile, start: date, count: int = 3, horizon: int = 21) -> List[date]:
    """The next few dates the business is open, for "we're closed then, how about..." replies."""
    out, d = [], start
    for _ in range(horizon):
        if profile.hours_on(d):
            out.append(d)
            if len(out) >= count:
                break
        d += timedelta(days=1)
    return out
