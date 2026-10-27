"""When a caller needs a human: live transfer, or take a message?

The landing page promises Vaani "transfers live to your team". That promise
only holds when there is a number to dial and a person likely to pick up -
otherwise the caller sits through a ringing <Dial> that nobody answers, which
is worse than an honest "I'll have someone call you back".

The voice webhook turns the decision into TwiML:
    "transfer" -> <Dial> profile.transfer_phone
    "message"  -> record the request as a callback on the call log
"""

from datetime import datetime

from services.business_profile import BusinessProfile

TRANSFER = "transfer"
MESSAGE = "message"


def escalation_route(profile: BusinessProfile, now: datetime, reason: str = "") -> str:
    """Decide how to hand a caller to a human.

    Args:
        profile: the business - see transfer_phone, is_open_at(), hours_on()
        now:     current time at the business (timezone-aware)
        reason:  why the caller is being escalated, as the LLM phrased it
                 (e.g. "caller asked for a human", "billing dispute")

    Returns TRANSFER or MESSAGE.

    Policy: transfer only when there is a number to dial AND the business is
    open right now. After hours the transfer phone is usually a front-desk
    line nobody is at; ringing it for 25 seconds and then taking a message
    anyway wastes the caller's time. `reason` is recorded on the call log for
    the owner, but deliberately does not change routing: a policy that
    guesses from the model's free-text summary would route unpredictably.
    """
    if not (profile.transfer_phone or "").strip():
        return MESSAGE
    if not profile.is_open_at(now):
        return MESSAGE
    return TRANSFER
