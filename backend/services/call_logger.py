"""Persist conversations as call logs so the dashboard has something to show.

Every exchange - a real phone call or a session in the web demo - is upserted
into `call_logs` keyed by its session id, accumulating the transcript as the
conversation goes. Without this the dashboard can only ever report zeros.

Failures here must never break a live call, so callers should treat this as
best-effort: log and continue.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from db import BusinessSQL, CallLogSQL

logger = logging.getLogger(__name__)

# Anonymous web-demo traffic is attributed here so it never mixes with a real
# customer's numbers.
DEMO_BUSINESS_ID = "demo-business"

# Maps the agent's intent onto the call_outcome enum in the schema.
_OUTCOME_BY_INTENT = {
    "BOOK_APPOINTMENT": "booked",
    "ANSWER_FAQ": "completed_faq",
    "ESCALATE_TO_HUMAN": "escalated",
    "TRANSFER": "transferred",
}


async def get_or_create_demo_business(session) -> str:
    """The catch-all business for unauthenticated demo conversations."""
    existing = (
        await session.execute(select(BusinessSQL).where(BusinessSQL.id == DEMO_BUSINESS_ID))
    ).scalar_one_or_none()

    if existing:
        return existing.id

    session.add(BusinessSQL(
        id=DEMO_BUSINESS_ID,
        name="Vaani Demo",
        phone="",
        email="demo@vaani.local",
        status="active",
        currency="INR",
    ))
    await session.flush()
    return DEMO_BUSINESS_ID


# A call is summarised by the most significant thing that happened in it, not
# by whatever its last turn happened to be. Without this ordering a booking
# followed by "thanks, bye" gets filed as a plain FAQ answer.
_OUTCOME_RANK = {
    "failed": 0,
    "completed_faq": 1,
    "transferred": 2,
    "escalated": 3,
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


async def record_turn(
    session,
    session_id: str,
    business_id: str,
    transcript: List[Dict[str, Any]],
    intent_name: Optional[str] = None,
    escalated: bool = False,
    customer_phone: Optional[str] = None,
) -> Optional[str]:
    """Create or update the call log for this session. Returns its id."""
    try:
        row = (
            await session.execute(select(CallLogSQL).where(CallLogSQL.call_sid == session_id))
        ).scalar_one_or_none()

        now = datetime.now(timezone.utc)

        if row is None:
            row = CallLogSQL(
                id=uuid.uuid4().hex,
                business_id=business_id,
                call_sid=session_id,
                start_time=now,
                customer_phone=customer_phone,
            )
            session.add(row)

        # Clients that replay the full history each turn (the web demo) send a
        # complete transcript; clients that only send the latest exchange (the
        # Twilio webhook) send a partial one. Take whichever is longer, and
        # append the delta when the incoming batch is the shorter of the two,
        # so a call log never loses turns it already had.
        existing = row.transcript or []
        if len(transcript) >= len(existing):
            row.transcript = transcript
        else:
            row.transcript = existing + transcript

        row.end_time = now
        row.outcome = merge_outcome(row.outcome, outcome_for(intent_name, escalated))
        # Escalation is latching too: it happened, even if later turns did not.
        row.escalation_requested = bool(row.escalation_requested or escalated)

        start = row.start_time or now
        # SQLite hands back naive datetimes; normalise before subtracting.
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        row.duration_seconds = max(0, int((now - start).total_seconds()))

        await session.commit()
        return row.id

    except Exception as e:
        # Never let bookkeeping take down a conversation.
        logger.warning("Failed to record call log for %s: %s", session_id, e)
        try:
            await session.rollback()
        except Exception:
            pass
        return None
