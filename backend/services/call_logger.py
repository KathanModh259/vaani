"""Call logs: the dashboard's record of each call, and the call's memory.

Every exchange - a real phone call or a session in the web demo - lives in
one `call_logs` row keyed by its session id (Twilio's CallSid on the phone).
The encrypted transcript on that row is also what the agent reads back at the
start of each turn: Twilio calls the webhook once per utterance with no
state of its own, so without this the receptionist forgot everything the
caller said ten seconds earlier.

Transcript entries: {"speaker": "caller" | "agent" | "event", "text", "timestamp"}.
"event" entries record what the system actually did (booked, cancelled,
transferred) so the owner can audit it and the model can recap it.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from db import BusinessSQL, CallLogSQL, FAQSQL, ServiceSQL, BusinessHoursSQL
from services.crypto import decrypt_json, encrypt_json

logger = logging.getLogger(__name__)

# Anonymous web-demo traffic is attributed here so it never mixes with a real
# customer's numbers.
DEMO_BUSINESS_ID = "demo-business"

# A runaway call (or a script hammering the demo) must not grow one row forever.
MAX_TRANSCRIPT_TURNS = 400

# Maps the old rules-engine intent names onto the call_outcome enum.
_OUTCOME_BY_INTENT = {
    "BOOK_APPOINTMENT": "booked",
    "ANSWER_FAQ": "completed_faq",
    "ESCALATE_TO_HUMAN": "escalated",
    "TRANSFER": "transferred",
}

# A call is summarised by the most significant thing that happened in it, not
# by whatever its last turn happened to be. Without this ordering a booking
# followed by "thanks, bye" gets filed as a plain FAQ answer.
_OUTCOME_RANK = {
    "failed": 0,
    "completed_faq": 1,
    "escalated": 2,
    "transferred": 3,
    "booked": 4,
}


def outcome_for(intent_name: Optional[str], escalated: bool = False) -> str:
    if escalated:
        return "escalated"
    return _OUTCOME_BY_INTENT.get((intent_name or "").upper(), "completed_faq")


def merge_outcome(existing: Optional[str], incoming: str) -> str:
    """Keep the more significant of the two outcomes."""
    if not existing:
        return incoming
    return max(existing, incoming, key=lambda o: _OUTCOME_RANK.get(o, 1))


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def transcript_of(row: Optional[CallLogSQL]) -> List[Dict[str, Any]]:
    if row is None:
        return []
    return decrypt_json(row.transcript, default=[]) or []


def turn(speaker: str, text: str) -> Dict[str, Any]:
    return {"speaker": speaker, "text": text, "timestamp": datetime.now(timezone.utc).isoformat()}


async def find_call(session, call_sid: str) -> Optional[CallLogSQL]:
    return (await session.execute(
        select(CallLogSQL).where(CallLogSQL.call_sid == call_sid)
    )).scalar_one_or_none()


async def start_call(
    session,
    call_sid: str,
    business_id: str,
    customer_phone: Optional[str] = None,
    opening_turns: Optional[List[Dict[str, Any]]] = None,
) -> CallLogSQL:
    """Return the call row, creating (and committing) it if new.

    Committed immediately so the id is durable before any booking points at
    it - a later rollback (e.g. a slot taken mid-call) must not erase the call.
    """
    row = await find_call(session, call_sid)
    if row is not None:
        return row
    now = datetime.now(timezone.utc)
    row = CallLogSQL(
        id=uuid.uuid4().hex,
        business_id=business_id,
        call_sid=call_sid,
        start_time=now,
        end_time=now,
        customer_phone=customer_phone,
        transcript=encrypt_json(list(opening_turns or [])),
        outcome="completed_faq",
        duration_seconds=0,
        escalation_requested=False,
    )
    session.add(row)
    await session.commit()
    return row


async def append_turns(
    session,
    call_sid: str,
    new_turns: List[Dict[str, Any]],
    outcome: Optional[str] = None,
    escalated: bool = False,
) -> None:
    """Append to the transcript and fold in this turn's outcome.

    Re-reads the row rather than trusting an ORM object from earlier in the
    request: a booking rollback mid-turn expires loaded instances, and
    touching an expired attribute under asyncio raises instead of reloading.
    """
    try:
        row = await find_call(session, call_sid)
        if row is None:
            logger.warning("append_turns: no call log for %s", call_sid)
            return
        now = datetime.now(timezone.utc)
        merged = (transcript_of(row) + list(new_turns))[-MAX_TRANSCRIPT_TURNS:]
        # Encrypted before it reaches the database - transcripts routinely
        # contain personal details a caller spoke aloud.
        row.transcript = encrypt_json(merged)
        row.end_time = now
        if outcome:
            row.outcome = merge_outcome(row.outcome, outcome)
        # Escalation is latching: it happened, even if later turns did not.
        row.escalation_requested = bool(row.escalation_requested or escalated)
        start = _aware(row.start_time) or now
        row.duration_seconds = max(row.duration_seconds or 0, int((now - start).total_seconds()))
        await session.commit()
    except Exception as e:
        # Never let bookkeeping take down a live conversation.
        logger.warning("Failed to record call log for %s: %s", call_sid, e)
        try:
            await session.rollback()
        except Exception:
            pass


async def read_meta_by_id(session, call_log_id: str) -> dict:
    """The call's metadata blob, addressed by row id; {} when unknown.

    The conversation code carries the row id (bookings reference it), while
    Twilio only ever knows its own SID, so this cannot reuse find_call().
    The booking draft lives here rather than in the transcript: it is state
    the system needs to resume a half-finished booking, not something the
    caller said, and it must survive a restart between two phone turns.
    """
    row = await session.get(CallLogSQL, call_log_id)
    return dict(row.call_metadata or {}) if row is not None else {}


async def write_meta_by_id(session, call_log_id: str, key: str, value) -> None:
    """Set (or delete, with None) one metadata key on the call row."""
    row = await session.get(CallLogSQL, call_log_id)
    if row is None:
        return
    meta = dict(row.call_metadata or {})
    if value is None:
        meta.pop(key, None)
    else:
        meta[key] = value
    row.call_metadata = meta  # reassign so SQLAlchemy sees the JSON change
    await session.commit()


async def bump_meta(session, call_sid: str, key: str, reset: bool = False) -> int:
    """Increment (or reset) a per-call counter in call_metadata; returns it.

    Used for consecutive silences: Twilio re-posts an empty SpeechResult each
    time the caller says nothing, and without a cap a silent line re-prompts
    forever, billing minutes the whole time.
    """
    row = await find_call(session, call_sid)
    if row is None:
        return 0
    meta = dict(row.call_metadata or {})
    meta[key] = 0 if reset else int(meta.get(key, 0)) + 1
    row.call_metadata = meta  # reassign so SQLAlchemy sees the JSON change
    await session.commit()
    return meta[key]


async def mark_callback(session, call_sid: str, reason: str) -> Optional[CallLogSQL]:
    """Record that the caller is owed a callback (e.g. a transfer nobody answered)."""
    row = await find_call(session, call_sid)
    if row is None:
        return None
    row.escalation_requested = True
    # A transfer nobody answered is not a handled call: downgrade it, or the
    # dashboard files a caller who is still waiting as "Transferred".
    row.outcome = "escalated" if row.outcome == "transferred" else merge_outcome(row.outcome, "escalated")
    row.follow_up_done_at = None
    row.transcript = encrypt_json(transcript_of(row) + [turn("event", f"Callback requested: {reason}")])
    await session.commit()
    return row


async def finish_call(session, call_sid: str, duration_seconds: Optional[int]) -> None:
    """Twilio's status callback: the carrier's duration is the billable truth."""
    row = await find_call(session, call_sid)
    if row is None:
        return
    row.end_time = datetime.now(timezone.utc)
    if duration_seconds is not None:
        row.duration_seconds = duration_seconds
    await session.commit()


# ------------------------------------------------------------------ demo

DEMO_SERVICES = [
    ("Dental check-up", "Examination and advice", 30, 50000),
    ("Teeth cleaning", "Scaling and polishing", 45, 120000),
    ("Root canal consultation", "Assessment for root canal treatment", 30, 80000),
]
DEMO_FAQS = [
    ("How can I pay?", "You can pay by card, cash or UPI at the time of your visit."),
    ("What is your cancellation policy?",
     "We ask for 24 hours notice to cancel or reschedule, and refunds are given in full within that window."),
    ("Is there parking?", "Yes, there is free parking right outside the clinic."),
    ("Where are you located?", "We're on CG Road in Ahmedabad, next to the Navrangpura bus stop."),
]


async def get_or_create_demo_business(session) -> str:
    """The catch-all business for unauthenticated demo conversations.

    Seeded with a sample dental clinic so the public demo can show real
    availability and booking. This is where the clinic's prices belong -
    they used to be hard-coded into the agent and heard by every client's
    callers.
    """
    global _demo_ready
    if _demo_ready:
        return DEMO_BUSINESS_ID

    from services.business_profile import seed_default_hours

    existing = (await session.execute(
        select(BusinessSQL).where(BusinessSQL.id == DEMO_BUSINESS_ID)
    )).scalar_one_or_none()
    if existing is None:
        session.add(BusinessSQL(
            id=DEMO_BUSINESS_ID, name="Bright Smile Dental", phone="", email="demo@vaani.local",
            status="active", currency="INR", time_zone="Asia/Kolkata",
            about="Bright Smile Dental is a family dental clinic in Ahmedabad.",
            booking_enabled=True,
        ))
        await session.flush()

    has = lambda model: select(model.id).where(model.business_id == DEMO_BUSINESS_ID).limit(1)  # noqa: E731
    if (await session.execute(has(BusinessHoursSQL))).first() is None:
        seed_default_hours(session, DEMO_BUSINESS_ID)
    if (await session.execute(has(ServiceSQL))).first() is None:
        for name, desc, minutes, price in DEMO_SERVICES:
            session.add(ServiceSQL(id=uuid.uuid4().hex, business_id=DEMO_BUSINESS_ID, name=name,
                                   description=desc, duration_minutes=minutes, price=price,
                                   buffer_minutes=0))
    if (await session.execute(has(FAQSQL))).first() is None:
        for q, a in DEMO_FAQS:
            session.add(FAQSQL(id=uuid.uuid4().hex, business_id=DEMO_BUSINESS_ID, question=q, answer=a))
    await session.commit()
    _demo_ready = True
    return DEMO_BUSINESS_ID


_demo_ready = False
