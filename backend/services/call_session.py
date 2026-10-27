"""One conversational turn, end to end, for both the phone and the web demo.

    load profile -> find/start call log -> read its transcript (memory)
    -> agent.handle_turn -> append caller line, events, reply
    -> alert the owner about bookings / promised callbacks -> result

Both routes go through here so a phone call and a browser session behave
identically, and so there is one place that guarantees the transcript records
what the system did, not just what it said.
"""

import logging
from typing import Optional

from llm_agent.agent import TurnResult, get_agent
from services import owner_alerts
from services.business_profile import load_profile
from services.call_logger import append_turns, start_call, transcript_of, turn

logger = logging.getLogger(__name__)


class UnknownBusiness(Exception):
    pass


async def run_turn(
    session,
    business_id: str,
    call_sid: str,
    user_text: str,
    channel: str,
    caller_phone: Optional[str] = None,
) -> TurnResult:
    profile = await load_profile(session, business_id)
    if profile is None:
        raise UnknownBusiness(business_id)

    call = await start_call(session, call_sid, business_id, customer_phone=caller_phone)
    call_id = call.id
    already_escalated = bool(call.escalation_requested)
    history = transcript_of(call)

    result = await get_agent().handle_turn(
        session, profile, history, user_text,
        channel=channel, caller_phone=caller_phone, call_log_id=call_id,
    )

    new_turns = [turn("caller", user_text)]
    new_turns += [turn("event", e) for e in result.events]
    new_turns.append(turn("agent", result.reply))
    await append_turns(session, call_sid, new_turns, outcome=result.outcome, escalated=result.escalated)

    try:
        for booked in result.booked:
            owner_alerts.booking_alert(profile, booked, channel)
        # One alert per call: a caller asking three times is one callback.
        if result.callback_reason and not already_escalated:
            owner_alerts.callback_alert(profile, caller_phone, result.callback_reason, call_id)
    except Exception as e:
        logger.warning("Owner alert failed for %s: %s", call_sid, e)
    return result
