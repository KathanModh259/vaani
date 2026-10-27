"""The LLM tool-calling loop, driven by a scripted fake OpenAI client.

No network: the fake returns canned tool calls / replies in order, and the
tests check what actually reached the database and the transcript.
"""

import json
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from llm_agent import llm_provider
from scripts.config import settings


def tool_call(name, **args):
    return NS(id=f"call_{name}", type="function", function=NS(name=name, arguments=json.dumps(args)))


def reply(text=None, *calls):
    return NS(choices=[NS(message=NS(content=text, tool_calls=list(calls) or None))])


class FakeOpenAI:
    def __init__(self, script):
        self.script, self.seen = list(script), []
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kwargs):
        self.seen.append(kwargs["messages"][:])
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def fake_llm(monkeypatch):
    def install(*script):
        fake = FakeOpenAI(script)
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(llm_provider, "_client", fake)
        return fake
    return install


def bookable_date():
    """A Monday-Saturday at least two days out, in the demo clinic's zone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    d = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=2)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d.isoformat()


async def appointments(session):
    from sqlalchemy import select
    from db import AppointmentSQL
    return (await session.execute(select(AppointmentSQL))).scalars().all()


async def transcript(session, sid):
    from services.call_logger import find_call, transcript_of
    return transcript_of(await find_call(session, sid))


async def test_booking_through_tools_writes_a_real_appointment(client, session, fake_llm):
    day = bookable_date()
    fake = fake_llm(
        reply(None, tool_call("check_availability", date=day, service_name="check-up")),
        reply(None, tool_call("book_appointment", service_name="Dental check-up", date=day, time="11:00",
                              customer_name="Asha Patel", customer_phone="9876543210")),
        reply("You're booked for a check-up at 11 AM. Anything else?"),
    )
    r = (await client.post("/api/chat", json={"message": "book me a check-up at 11"})).json()

    assert r["source"] == "llm"
    appts = await appointments(session)
    assert len(appts) == 1 and appts[0].customer_name == "Asha Patel"
    assert appts[0].customer_phone == "+919876543210" and appts[0].source == "web"

    # The model saw real slots from the calendar, including 11:00.
    tool_msgs = [m for m in fake.seen[1] if m["role"] == "tool"]
    assert f"{day} 11:00" in json.loads(tool_msgs[0]["content"])["slots"]

    turns = await transcript(session, r["session_id"])
    assert [t["speaker"] for t in turns] == ["caller", "event", "agent"]
    assert turns[1]["text"].startswith("Booked Dental check-up for Asha Patel")


async def test_hallucinated_booking_is_corrected(client, session, fake_llm):
    fake = fake_llm(
        reply("Great, you're booked for tomorrow at 10!"),       # no tool call - a lie
        reply("Before I book, could I take your name and number?"),
    )
    r = (await client.post("/api/chat", json={"message": "book me tomorrow at 10"})).json()

    assert r["response"] == "Before I book, could I take your name and number?"
    assert await appointments(session) == []
    correction = fake.seen[1][-1]
    assert correction["role"] == "system" and "no appointment has been booked" in correction["content"]


async def test_recapping_an_earlier_booking_is_not_flagged(client, session, fake_llm):
    day = bookable_date()
    fake_llm(
        reply(None, tool_call("book_appointment", service_name="check-up", date=day, time="12:00",
                              customer_name="Ravi", customer_phone="9123456780")),
        reply("Done, you're booked."),
    )
    sid = (await client.post("/api/chat", json={"message": "book check-up at noon"})).json()["session_id"]

    fake = fake_llm(reply("Yes, you're booked for noon."))
    r = (await client.post("/api/chat", json={"message": "am I booked?", "session_id": sid})).json()
    assert r["response"] == "Yes, you're booked for noon."
    assert len(fake.seen) == 1                       # no correction round
    # The earlier booking reached the model as an event, not as something it said.
    assert any(m["role"] == "system" and m["content"].startswith("[Earlier in this call] Booked")
               for m in fake.seen[0])


async def test_slot_taken_returns_alternatives_not_a_booking(client, session, fake_llm):
    day = bookable_date()
    for name in ("First", "Second"):
        fake = fake_llm(
            reply(None, tool_call("book_appointment", service_name="check-up", date=day, time="15:00",
                                  customer_name=name, customer_phone="9876500000")),
            reply("ok"),
        )
        await client.post("/api/chat", json={"message": "book 3pm"})
    result = json.loads([m for m in fake.seen[1] if m["role"] == "tool"][0]["content"])
    assert result["booked"] is False and result["reason"] == "taken" and result["alternatives"]
    assert len(await appointments(session)) == 1


async def test_provider_failure_after_booking_still_confirms_truthfully(client, session, fake_llm):
    day = bookable_date()
    fake_llm(
        reply(None, tool_call("book_appointment", service_name="check-up", date=day, time="10:00",
                              customer_name="Meera", customer_phone="9000000001")),
        TimeoutError("provider timed out"),
    )
    r = (await client.post("/api/chat", json={"message": "book 10am"})).json()
    assert r["response"].startswith("You're booked for Dental check-up")
    assert len(await appointments(session)) == 1


async def test_provider_down_falls_back_to_rules(client, fake_llm):
    fake_llm(ConnectionError("no route to host"))
    r = (await client.post("/api/chat", json={"message": "hello"})).json()
    assert r["source"] == "rules" and "Bright Smile Dental" in r["response"]


def test_booking_claim_detector():
    for claim in ("You're booked for 10.", "I've booked that for you", "Your appointment is confirmed",
                  "आपका अपॉइंटमेंट बुक हो गया है"):
        assert llm_provider.claims_booking(claim), claim
    for fine in ("That slot is booked, how about 11?", "I can book that for you.",
                 "Would you like me to book it?"):
        assert not llm_provider.claims_booking(fine), fine
