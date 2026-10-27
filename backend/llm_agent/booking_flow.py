"""Booking without a model.

The rules engine used to refuse to book: it could not reliably pull a service,
a date, a time and a name out of free speech, so it promised a callback. That
is honest, but it means the demo only books when someone is paying a model
provider - and a receptionist that stops working when a key expires is not a
receptionist.

This module is the deterministic path. It runs the same slot-filling dialogue a
human receptionist runs (which service, which day, what time, your name, your
number), parses each answer with plain functions, and then calls the *same*
`book_appointment` tool the model calls. So:

  * the booking is real - same validation, same conflict check, same row;
  * nothing is claimed that a tool did not confirm;
  * it works offline, for free, with no key at all;
  * when an open-weight model is configured it takes over and this never runs.

State lives in `call_logs.metadata["booking_draft"]`, not in the transcript, so
a phone call that spans two server processes still resumes, and the caller
never has to repeat what they already said.
"""

from __future__ import annotations

import copy
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from services import booking
from services.business_profile import BusinessProfile, ServiceInfo, next_open_days
from services.call_logger import read_meta_by_id, write_meta_by_id
from services.phone import normalize_phone

logger = logging.getLogger(__name__)

DRAFT_KEY = "booking_draft"
MAX_ATTEMPTS = 3          # same question asked three times -> get a human
MAX_SUGGESTIONS = 4

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

# A word the caller might put in front of their name.
_NAME_PREFIX = re.compile(
    r"^(?:my name is|my name's|name is|name's|name|this is|it is|it's|i am|i'm|myself|"
    r"call me|mera naam|mera naam hai|naam)\s+",
    re.IGNORECASE,
)
_NOT_A_NAME = re.compile(
    r"(book|appoint|tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening|please|thanks|thank|service|checkup|check-up|cleaning|"
    r"cancel|reschedule|yes|no|okay|ok|hello|hi)\b",
    re.IGNORECASE,
)

# Noise that must never become a name on the booking.
_FILLER = {
    "hmm", "hmmm", "umm", "uh", "uhh", "er", "erm", "what", "sorry", "pardon", "again",
    "maybe", "later", "none", "nothing", "wait", "hold", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine", "ten", "yeah", "yep", "nope", "sure",
}


# ------------------------------------------------------------------ parsing

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def parse_service(text: str, profile: BusinessProfile) -> Optional[ServiceInfo]:
    """The service the caller named, if any.

    Token overlap rather than plain substring: "I need a cleaning on Friday"
    must find "Teeth cleaning" without also matching "check-up" on "check".
    """
    if not profile.services:
        return None
    best, best_score = None, 0
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    for service in profile.services:
        tokens = {w for w in re.findall(r"[a-z]+", service.name.lower()) if len(w) > 3}
        score = len(tokens & words)
        if score > best_score:
            best, best_score = service, score
    if best is not None:
        return best
    # Fall back to the profile's own matcher for phrasings we did not tokenise,
    # e.g. "root canal" for "Root canal consultation".
    return profile.find_service(text) if len(profile.services) == 1 else None


def parse_day(text: str, now: datetime) -> Optional[date]:
    """A calendar date said in English, Hindi or Hinglish. None if absent."""
    t = _clean(text).lower()
    if not t:
        return None

    if re.search(r"\bday after tomorrow\b|\bparso\b|\bparson\b", t):
        return now.date() + timedelta(days=2)
    if re.search(r"\btomorrow\b|\btomoro\b|\btmrw\b|\bkal\b|\bkal ke\b", t):
        return now.date() + timedelta(days=1)
    if re.search(r"\btoday\b|\baaj\b|\bright now\b", t):
        return now.date()

    iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", t)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    # "27 October", "27th of oct", "October 27"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+([a-z]{3,9})", t)
    if m and m.group(2) in _MONTHS:
        return _date_in_year(int(m.group(1)), _MONTHS[m.group(2)], now.date())
    m = re.search(r"\b([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m and m.group(1) in _MONTHS:
        return _date_in_year(int(m.group(2)), _MONTHS[m.group(1)], now.date())

    # 27/10, 27-10-2026
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
    if m:
        year = int(m.group(3)) if m.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            return _date_in_year(int(m.group(1)), int(m.group(2)), now.date(), year)
        except ValueError:
            return None

    # "on the 27th"
    m = re.search(r"\b(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", t)
    if m:
        day = int(m.group(1))
        candidate = _date_in_year(day, now.month, now.date())
        if candidate < now.date():
            month = now.month % 12 + 1
            year = now.year + (1 if now.month == 12 else 0)
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
        return candidate

    for name, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t):
            delta = (weekday - now.weekday()) % 7
            return now.date() + timedelta(days=delta)
    return None


def _date_in_year(day: int, month: int, today: date, year: Optional[int] = None) -> Optional[date]:
    """Resolve a day/month with no year to the next one that is not in the past."""
    for candidate_year in ((year,) if year else (today.year, today.year + 1)):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def parse_time(text: str) -> Optional[tuple[int, int]]:
    """(hour, minute) in 24-hour time, or None.

    Deliberately literal: "4pm" is 16:00, "16:30" is 16:30, and a bare "4"
    only counts when the caller said "at 4" or "4 baje" - otherwise a phone
    number would be read as an appointment.
    """
    t = _clean(text).lower()
    if not t:
        return None

    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|a\.m|p\.m)?", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        mer = (m.group(3) or "").replace(".", "")
        return _to_24(hour, minute, mer)

    m = re.search(r"\b(\d{1,2})\s*(am|pm|a\.m|p\.m)\b", t)
    if m:
        return _to_24(int(m.group(1)), 0, m.group(2).replace(".", ""))

    m = re.search(r"\b(?:at|by|around|aaj|kal|tomorrow|today)?\s*(\d{1,2})\s*(?:o'?clock|baje)\b", t)
    if m:
        return _bare_hour(int(m.group(1)))
    # A bare hour needs a cue word, otherwise the digits of a phone number
    # ("11 9988776655") would be read as an appointment. "around 4" and
    # "about 4" are how people actually ask, and mean the same as "at 4".
    m = re.search(r"\b(?:at|around|about|round|by)\s+(\d{1,2})\b(?!\s*[:.]\s*\d)", t)
    if m:
        return _bare_hour(int(m.group(1)))

    if re.search(r"\bnoon\b|\bmadhyahn\b", t):
        return (12, 0)
    if re.search(r"\bmidnight\b", t):
        return (0, 0)
    return None


def _to_24(hour: int, minute: int, meridiem: str) -> Optional[tuple[int, int]]:
    if minute > 59 or hour > 23:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return (hour, minute)


def _bare_hour(hour: int) -> Optional[tuple[int, int]]:
    if hour > 23:
        return None
    # "4" almost always means 4 in the afternoon for a business that opens at 9.
    if 1 <= hour <= 8:
        hour += 12
    return (hour, 0)


def parse_name(text: str, awaiting_name: bool) -> Optional[str]:
    """The caller's name. `awaiting_name` allows a bare answer ("Meera")."""
    t = _clean(text)
    if not t:
        return None

    match = re.search(r"(?:my name is|my name's|name is|this is|i am|i'm|myself|call me|naam)\s+([A-Za-z][A-Za-z .'-]{1,40})",
                      t, re.IGNORECASE)
    if match is None:
        # Dictated details: "..., name Asha, 99887 76655". The comma keeps
        # "the name of the clinic" from being read as a person.
        match = re.search(r",\s*name\s+([A-Za-z][A-Za-z .'-]{1,40})", t, re.IGNORECASE)
    candidate = match.group(1) if match else None

    if candidate is None and awaiting_name:
        stripped = _NAME_PREFIX.sub("", t)
        stripped = re.sub(r"^(?:yes|yeah|okay|ok|so|and)[,\s]+", "", stripped, flags=re.IGNORECASE)
        # "Meera Patel, 99887 76655" - the number is a separate field.
        stripped = re.sub(r"[,;]?\s*\+?\d[\d\s().-]{7,}\s*$", "", stripped).strip(" ,;")
        if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,40}", stripped):
            candidate = stripped

    if candidate is None:
        return None
    candidate = candidate.strip(" .,'-")
    if not candidate or len(candidate) > 40 or len(candidate) < 2:
        return None
    if re.search(r"\d", candidate) or _NOT_A_NAME.search(candidate):
        return None
    if candidate.lower().replace(" ", "") in _FILLER:
        return None
    return " ".join(part.capitalize() for part in candidate.split())


def _bare_day(text: str, now: datetime) -> Optional[date]:
    """A day-of-month as the whole answer: "27" to "Which day suits you?"."""
    match = re.fullmatch(r"\s*(\d{1,2})(?:st|nd|rd|th)?\s*", text or "")
    if not match:
        return None
    day = int(match.group(1))
    if not 1 <= day <= 31:
        return None
    for offset in (0, 1):
        month = (now.month - 1 + offset) % 12 + 1
        year = now.year + (1 if now.month + offset > 12 else 0)
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= now.date():
            return candidate
    return None


def _bare_clock(text: str) -> Optional[tuple[int, int]]:
    """A time as the whole answer: "4" to "What time works for you?"."""
    match = re.fullmatch(r"\s*(\d{1,2})\s*", text or "")
    if not match:
        return None
    return _bare_hour(int(match.group(1)))


def parse_phone(text: str) -> Optional[str]:
    """A phone number the caller dictated, in E.164 when it looks Indian.

    Digits are matched inside the original text, not after stripping it: a
    sentence like "cleaning on 2026-10-28 at 11, 9988776655" has a date, a
    time and a number, and stripping non-digits turns them into one
    15-digit monster that gets stored as the caller's phone.
    """
    for match in re.finditer(r"\+?\d[\d\s().-]{7,}\d", text or ""):
        chunk = match.group(0)
        digits = re.sub(r"\D", "", chunk)
        if chunk.lstrip().startswith("+"):
            if 8 <= len(digits) <= 15:
                return "+" + digits
            continue
        if len(digits) == 10 and digits[0] in "6789":
            return normalize_phone(chunk)
        if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
            return normalize_phone(chunk)
        if len(digits) == 12 and digits.startswith("91"):
            return normalize_phone(chunk)
        # "11 9988776655": a stray hour glued to a real number.
        if len(digits) == 12 and digits[2] in "6789" and digits[2:] == digits[-10:]:
            return normalize_phone(digits[-10:])
    return None


# ------------------------------------------------------------------- replies

def _spoken(dt: datetime) -> str:
    return dt.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _fmt_time(value) -> str:
    """A time-of-day for speech: 09:00 -> "9 AM", 17:30 -> "5:30 PM"."""
    return value.strftime("%I:%M %p").lstrip("0").replace(":00", "")


def _slot_line(slots: List[datetime]) -> str:
    times = [_clock(s) for s in slots[:MAX_SUGGESTIONS]]
    if len(times) <= 1:
        return ", ".join(times)
    return ", ".join(times[:-1]) + " or " + times[-1]


def _service_line(profile: BusinessProfile) -> str:
    names = [s.name for s in profile.services[:4]]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


async def _availability(profile: BusinessProfile, session, service: ServiceInfo, day: date):
    slots = await booking.free_slots(session, profile, service, day, limit=MAX_SUGGESTIONS + 2)
    if slots:
        return slots, None
    if not profile.hours_on(day):
        opens = next_open_days(profile, day + timedelta(days=1), count=2)
        if opens:
            return [], ("We're closed on " + day.strftime("%A") + ". How about "
                        + " or ".join(d.strftime("%A %d %B") for d in opens) + "?")
        return [], "We're closed on " + day.strftime("%A") + ". Which other day suits you?"
    later = await booking.next_available(session, profile, service, day, limit=MAX_SUGGESTIONS)
    if later:
        return [], (day.strftime("%A") + " is fully booked. The next openings are "
                    + ", ".join(f"{s.strftime('%A')} {_clock(s)}" for s in later) + ".")
    return [], (day.strftime("%A") + " is fully booked, and I don't have an opening in the next "
                "two weeks. Let me take your number and have the team call you.")


# ---------------------------------------------------------------- the flow

class BookingFlow:
    """Deterministic slot filling over the same booking tool the model uses."""

    async def maybe_handle(self, ctx, text: str, intent) -> Optional[str]:
        """Return a reply when this turn is part of a booking, else None.

        None means "not a booking turn" and lets the normal rules engine answer,
        so an FAQ asked midway through booking still gets the owner's answer.
        """
        from llm_agent.agent import Intent          # local: agent imports this module

        draft = await self._load(ctx)
        profile = ctx.profile

        probe_service = parse_service(text, profile)
        probe_day = parse_day(text, profile.now())
        probe_clock = parse_time(text)

        starting = intent == Intent.BOOK_APPOINTMENT
        if draft is None and not starting and intent in (Intent.UNKNOWN, Intent.CHECK_AVAILABILITY):
            # "consultation tomorrow at 4, name Meera" names no booking verb,
            # and "do you have a cleaning on Friday" reads as availability.
            # Both are bookings; neither should fall through to "didn't catch
            # that" just because the magic word was missing.
            starting = probe_service is not None and (probe_day is not None or probe_clock is not None)

        if draft is None and not starting:
            return None
        # A caller who changes their mind must be able to leave the flow.
        if draft is not None and intent in (Intent.CANCEL_APPOINTMENT, Intent.RESCHEDULE_APPOINTMENT):
            await self._clear(ctx)
            return None

        draft = dict(draft or {"asked": {}})
        awaiting = draft.get("stage")
        filled_now = set()

        # Merge everything this turn said into the draft.
        if probe_service is not None and not draft.get("service"):
            draft["service"] = probe_service.id
            filled_now.add("service")

        day = probe_day
        if day is None and awaiting == "date":
            day = _bare_day(text, profile.now())
        if day is not None:
            # A new day invalidates a time that was said before it.
            if draft.get("date") != day.isoformat():
                draft.pop("time", None)
            draft["date"] = day.isoformat()
            filled_now.add("date")

        clock = probe_clock
        if clock is None and awaiting == "time":
            clock = _bare_clock(text)
        if clock is not None:
            draft["time"] = f"{clock[0]:02d}:{clock[1]:02d}"
            filled_now.add("time")

        name = parse_name(text, awaiting_name=(awaiting == "name"))
        if name:
            draft["name"] = name
            filled_now.add("name")

        phone = parse_phone(text)
        if phone:
            draft["phone"] = phone
            filled_now.add("phone")
        elif ctx.caller_phone and not draft.get("phone"):
            draft["phone"] = ctx.caller_phone

        # A question that filled nothing is not a booking answer - let the
        # rules engine answer it, and keep the draft for the next turn.
        if "?" in text and not filled_now:
            return None

        reply = await self._advance(ctx, draft, text)

        if draft.get("handoff"):
            from llm_agent.tools import dispatch
            await dispatch(ctx, "transfer_to_human",
                          {"reason": f"booking not completed: {text[:120]}"})
            await self._clear(ctx)
            return reply

        # A confirmed booking clears the draft; do not write it back.
        if not ctx.booked:
            await self._save(ctx, draft)
        return reply

    async def _advance(self, ctx, draft: Dict[str, Any], text: str) -> str:
        profile = ctx.profile

        # A brand-new signup has opening hours but no service catalogue yet.
        # There is nothing valid to offer, so offering "We have ." and then
        # failing is worse than taking a callback. Same when the owner turned
        # booking off on purpose.
        if not profile.services or not profile.booking_enabled:
            draft["handoff"] = True
            return ("I can arrange that, but I don't have this business's booking "
                    "list in front of me - let me have someone from the team call "
                    "you back to confirm it.")

        service = next((s for s in profile.services if s.id == draft.get("service")), None)
        if service is None:
            return self._ask(draft, "service",
                             f"Happy to book that. We offer {_service_line(profile)}. Which one would you like?")

        if not draft.get("date"):
            opens = next_open_days(profile, profile.now().date(), count=2)
            when = " or ".join(d.strftime("%A") for d in opens) if opens else "another day"
            return self._ask(draft, "date", f"Which day suits you? We're open {when}.")

        day = date.fromisoformat(draft["date"])

        if not draft.get("time"):
            slots, problem = await _availability(profile, ctx.session, service, day)
            if problem:
                draft.pop("date", None)
                return self._ask(draft, "date", problem)
            if slots:
                draft["stage"] = "time"
                return (f"On {day.strftime('%A %d %B')} I can do {_slot_line(slots)}. "
                        f"What time works for you?")
            draft.pop("date", None)
            return self._ask(draft, "date", "I don't have an opening on that day. Which other day suits you?")

        # Check the time makes sense *before* asking for a name and number:
        # nobody should hand over personal details for a slot that cannot
        # exist. The booking tool still owns the authoritative check.
        problem = await self._slot_problem(ctx, service, day, draft["time"])
        if problem:
            draft.pop("time", None)
            return self._ask(draft, "time", problem)

        if not draft.get("name"):
            return self._ask(draft, "name", "Great. And what name should I put on the booking?")

        if not draft.get("phone"):
            return self._ask(draft, "phone", "And the best phone number to reach you on?")

        # Everything is present: the real booking tool validates and writes it.
        from llm_agent.tools import dispatch

        result = await dispatch(ctx, "book_appointment", {
            "service_name": service.name,
            "date": draft["date"],
            "time": draft["time"],
            "customer_name": draft["name"],
            "customer_phone": draft["phone"],
        })

        if result.get("booked"):
            await self._clear(ctx)
            return self._confirmed(result)

        reason = result.get("reason")
        say = result.get("say") or "I couldn't book that time."
        alternatives = result.get("alternatives") or []

        if reason == "missing_phone":
            draft.pop("phone", None)
            return self._ask(draft, "phone", "Could I take a phone number for the booking?")

        # A refusal is the interesting path: the slot went, or the day is gone.
        if alternatives:
            when = ", ".join(_fmt_slot(a) for a in alternatives[:MAX_SUGGESTIONS])
            draft.pop("time", None)
            draft["stage"] = "time"
            return f"{say} I can offer {when}. Would one of those work?"

        draft.pop("time", None)
        return self._ask(draft, "time", say)

    async def _slot_problem(self, ctx, service: ServiceInfo, day: date, clock: str) -> Optional[str]:
        """Why this date/time cannot be booked, phrased for the caller.

        Only the checks that are about the clock and the calendar: conflicts
        stay with book(), which re-checks under a row lock.
        """
        profile = ctx.profile
        window = profile.hours_on(day)
        if not window:
            slots, problem = await _availability(profile, ctx.session, service, day)
            return problem
        start = booking.parse_local(profile, day.isoformat(), clock)
        end = start + timedelta(minutes=service.duration_minutes)
        if start < window[0] or end > window[1]:
            slots, _ = await _availability(profile, ctx.session, service, day)
            offered = f" I can do {_slot_line(slots)}." if slots else ""
            return f"We're open {_fmt_time(window[0])} to {_fmt_time(window[1])} that day.{offered} What time suits you?"
        if start < profile.now() + booking.MIN_NOTICE:
            slots, _ = await _availability(profile, ctx.session, service, day)
            offered = f" I can do {_slot_line(slots)}." if slots else ""
            return f"We can't do a time that soon.{offered} What time works for you?"
        return None

    def _ask(self, draft: Dict[str, Any], stage: str, question: str) -> str:
        """Ask once, ask twice, then stop: a loop is worse than a callback."""
        asked = draft.setdefault("asked", {})
        asked[stage] = int(asked.get(stage, 0)) + 1
        draft["stage"] = stage
        if asked[stage] >= MAX_ATTEMPTS:
            # Caller abandons nothing: the route still records this call, and
            # the caller is told a person will follow up rather than being
            # asked the same question a fourth time.
            draft["handoff"] = True
            return ("Let me have someone from the team confirm this with you directly. "
                    "I've noted what you're looking for.")
        if asked[stage] == 2:
            return question + " (You can say something like \"tomorrow at 4\" or \"Friday 11 am\".)"
        return question

    @staticmethod
    def _confirmed(result: Dict[str, Any]) -> str:
        when = result.get("when") or ""
        price = result.get("price")
        ref = result.get("reference")
        line = f"You're booked for {result.get('service')} on {when}."
        if price:
            line += f" That's {price}, payable at the visit."
        if ref:
            line += f" Your reference is {ref}."
        return line + " Is there anything else?"

    # ------------------------------------------------------------- storage

    async def _load(self, ctx) -> Optional[Dict[str, Any]]:
        if not ctx.call_log_id:
            return getattr(ctx, "_booking_draft", None)
        meta = await read_meta_by_id(ctx.session, ctx.call_log_id)
        draft = meta.get(DRAFT_KEY)
        # Deep copy: the nested dicts belong to the JSON value SQLAlchemy
        # already committed. Mutating them in place changes the "old" value
        # too, so the flush sees no difference and silently writes nothing.
        return copy.deepcopy(draft) if isinstance(draft, dict) else None

    async def _save(self, ctx, draft: Dict[str, Any]) -> None:
        if not ctx.call_log_id:
            ctx._booking_draft = draft      # single-turn use (tests, no call log)
            return
        await write_meta_by_id(ctx.session, ctx.call_log_id, DRAFT_KEY, copy.deepcopy(draft))

    async def _clear(self, ctx) -> None:
        if not ctx.call_log_id:
            ctx._booking_draft = None
            return
        await write_meta_by_id(ctx.session, ctx.call_log_id, DRAFT_KEY, None)


def _fmt_slot(value: Any) -> str:
    try:
        return _spoken(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return str(value)


_flow = BookingFlow()


def get_flow() -> BookingFlow:
    return _flow