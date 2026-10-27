"""The rules fallback: what callers hear when no LLM is configured or it fails.

It must answer from the business's own settings and never pretend to book.
"""

from services.business_profile import BusinessProfile, DayHours, ServiceInfo
from datetime import time

from llm_agent.agent import Intent, ReceptionistAgent, extract_intent


def _profile(**kw) -> BusinessProfile:
    base = dict(
        id="biz", name="Sharma Physio",
        hours={d: DayHours(True, time(10), time(19)) for d in range(5)},
        services=[ServiceInfo("s1", "Physiotherapy session", "", 45, 90000, 0)],
        faqs=[("Do you have parking?", "Yes, free parking behind the building.")],
    )
    base.update(kw)
    return BusinessProfile(**base)


async def _reply(text, profile=None, history=None):
    return await ReceptionistAgent().handle_turn(None, profile or _profile(), history or [], text, channel="web")


def test_intent_basics():
    assert extract_intent("I want to book an appointment") == Intent.BOOK_APPOINTMENT
    assert extract_intent("What are your hours?") == Intent.ANSWER_FAQ
    assert extract_intent("xyzzy flurbo garble") == Intent.UNKNOWN
    # "in person" is not a request for a human.
    assert extract_intent("can I come in person") != Intent.ESCALATE_TO_HUMAN


async def test_hours_come_from_the_business_not_the_code():
    r = await _reply("what are your opening hours")
    assert "10 AM to 7 PM" in r.reply
    assert "9 AM" not in r.reply  # the old hard-coded answer


async def test_prices_come_from_the_business():
    r = await _reply("how much does it cost")
    assert "900 rupees" in r.reply
    assert "1,200" not in r.reply  # the demo clinic's cleaning price


async def test_owner_faq_is_used():
    r = await _reply("is there parking nearby")
    assert "free parking behind the building" in r.reply


async def test_faq_match_ignores_filler_words():
    """"what are your hours" shares {what, your} with this FAQ - not a match."""
    p = _profile(faqs=[("What is your cancellation policy?", "24 hours notice please.")])
    r = await _reply("what are your hours", p)
    assert "10 AM to 7 PM" in r.reply and "notice" not in r.reply


async def test_no_invented_prices_when_none_listed():
    r = await _reply("how much is it", _profile(services=[]))
    assert "rupees" not in r.reply


async def test_greeting_uses_business_name():
    r = await _reply("hello")
    assert "Sharma Physio" in r.reply
    assert r.outcome == "completed_faq"


async def test_goodbye_ends_the_call():
    r = await _reply("thanks, bye")
    assert r.end_call is True
