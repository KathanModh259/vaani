"""The receptionist: one caller utterance in, one reply (and its effects) out.

Three answer paths, in order of preference:

  1. an open-weight model, driving the tools in llm_agent/tools.py;
  2. the deterministic booking flow (llm_agent/booking_flow.py), which slots
     fills a booking with plain parsing and calls the same tools - so a
     deployment with no model key still books real appointments;
  3. the keyword rules engine below, which answers FAQs from the owner's own
     profile and hands anything else to a human.

Paths 2 and 3 never claim a booking a tool did not commit.

Nothing here reads or writes the call log; services/call_session.py owns the
turn lifecycle (load history, run this, persist the result).
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from llm_agent import booking_flow, llm_provider
from llm_agent.tools import TOOL_SPECS, TurnContext, dispatch
from services.booking import BookedAppointment
from services.business_profile import BusinessProfile

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    BOOK_APPOINTMENT = "book_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    CHECK_AVAILABILITY = "check_availability"
    ANSWER_FAQ = "answer_faq"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    UNKNOWN = "unknown"


@dataclass
class TurnResult:
    reply: str
    source: str                       # "llm" | "rules"
    outcome: str                      # call_outcome enum value
    escalated: bool = False
    transfer_to: Optional[str] = None
    end_call: bool = False
    events: List[str] = field(default_factory=list)
    booked: List[BookedAppointment] = field(default_factory=list)
    # Set when the caller was promised a callback (escalated, not transferred).
    callback_reason: Optional[str] = None


# An ordered list, not a dict: the first match wins, so priority is the whole
# point and the same intent can appear at two priorities. With BOOK first,
# "reschedule my appointment" matched on "appointment" and "cancel my
# booking" on "book", so neither reschedule nor cancel could ever fire.
_INTENT_PATTERNS = [
    # An explicit request for a human outranks everything - a frustrated
    # caller must never be routed into booking because they said "appointment".
    (Intent.ESCALATE_TO_HUMAN, [
        "human", "agent", "representative", "transfer", "escalate",
        "talk to", "speak to", "speak with", "real person", "someone else",
        "manager", "supervisor",
    ]),
    # Questions *about* a policy come before the action they name, or
    # "what's your cancellation policy" starts cancelling a booking.
    (Intent.ANSWER_FAQ, ["policy", "refund", "do you take", "do you accept", "how do i pay",
                         "what do you charge", "insurance"]),
    (Intent.CANCEL_APPOINTMENT, ["cancel", "call it off", "don't need it", "dont need it"]),
    (Intent.RESCHEDULE_APPOINTMENT, ["reschedule", "postpone", "move my", "move the",
                                     "change my", "change the", "shift my", "different time"]),
    (Intent.CHECK_AVAILABILITY, ["available", "availability", "free", "slots",
                                 "open on", "are you open", "opening"]),
    (Intent.BOOK_APPOINTMENT, ["book", "schedule", "reserve", "appointment", "come in"]),
    (Intent.ANSWER_FAQ, ["hours", "how much", "price", "cost", "when", "what time",
                         "payment", "faq", "card", "cash", "upi", "open", "where", "address"]),
]

GREETINGS = ("hello", "hi", "hey", "namaste", "namaskar", "good morning",
             "good afternoon", "good evening", "how are you", "are you there")
COURTESIES = ("thank", "thanks", "shukriya", "dhanyavad", "bye", "goodbye", "that's all", "thats all")


def extract_intent(text: str) -> Intent:
    t = (text or "").lower()
    for intent, patterns in _INTENT_PATTERNS:
        if any(p in t for p in patterns):
            return intent
    return Intent.UNKNOWN


# Filler that appears in every question. Without this, "what are your hours"
# matches the FAQ "What is your cancellation policy?" on {what, your}.
_STOPWORDS = {
    "the", "and", "you", "your", "are", "what", "how", "can", "for", "have", "has", "there",
    "any", "with", "does", "this", "that", "from", "when", "where", "will", "would", "which",
    "who", "why", "about", "get", "much", "many", "please", "tell", "need", "want", "like",
    "our", "was", "were", "been", "they", "them", "its", "also", "just", "some", "near", "nearby",
}


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9ऀ-ॿ઀-૿]+", s.lower())
            if len(w) > 2 and w not in _STOPWORDS}


def _best_faq(profile: BusinessProfile, question: str) -> Optional[str]:
    """Owner's FAQ whose question shares the most words with the caller's."""
    asked = _words(question)
    best, score = None, 0
    for q, a in profile.faqs:
        overlap = len(asked & _words(q))
        if overlap > score:
            best, score = a, overlap
    return best if score >= 1 else None


class ReceptionistAgent:
    async def handle_turn(
        self,
        session,
        profile: BusinessProfile,
        history: List[Dict[str, Any]],
        user_text: str,
        channel: str = "phone",
        caller_phone: Optional[str] = None,
        call_log_id: Optional[str] = None,
    ) -> TurnResult:
        ctx = TurnContext(session=session, profile=profile, channel=channel,
                          caller_phone=caller_phone, call_log_id=call_log_id)

        reply, source = None, "rules"
        if llm_provider.is_configured():
            earlier_booking = any(t.get("speaker") == "event" and str(t.get("text", "")).startswith(("Booked", "Rescheduled"))
                                  for t in history)
            messages = llm_provider.build_messages(
                history, user_text, profile.name, profile.prompt_context(),
                channel=channel, caller_phone=caller_phone,
            )
            reply = await llm_provider.run_agent(
                messages, TOOL_SPECS,
                dispatch=lambda name, args: dispatch(ctx, name, args),
                booking_made=lambda: earlier_booking or bool(ctx.booked),
            )
            if reply:
                source = "llm"

        if not reply:
            # A tool already did something real this turn (booked, then the
            # provider timed out on the follow-up). Say what actually
            # happened rather than restarting the conversation.
            reply = self._reply_for_effects(ctx)
        if not reply:
            # No model. Try the deterministic booking dialogue: it is the only
            # path that can turn "tomorrow at 4" into a real row without a
            # provider, and it declines politely when the turn is not about
            # booking.
            intent = extract_intent(user_text)
            reply = await booking_flow.get_flow().maybe_handle(ctx, user_text, intent)
        if not reply:
            reply = await self._rules_reply(ctx, history, user_text)

        return TurnResult(
            reply=reply,
            source=source,
            outcome=self._outcome(ctx),
            escalated=ctx.escalated,
            transfer_to=ctx.transfer_to,
            end_call=ctx.end_call,
            events=list(ctx.events),
            booked=list(ctx.booked),
            callback_reason=(ctx.escalation_reason or "caller asked to be called back")
            if ctx.escalated and not ctx.transfer_to else None,
        )

    @staticmethod
    def _outcome(ctx: TurnContext) -> str:
        if ctx.transfer_to:
            return "transferred"
        if ctx.booked:
            return "booked"
        if ctx.escalated:
            return "escalated"
        return "completed_faq"

    @staticmethod
    def _reply_for_effects(ctx: TurnContext) -> Optional[str]:
        if ctx.booked:
            b = ctx.booked[-1]
            when = b.start.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")
            return f"You're booked for {b.service.name} on {when}. Is there anything else?"
        if ctx.transfer_to:
            return "Let me connect you to a member of the team now."
        if ctx.escalated:
            return "I'll ask a member of the team to call you back as soon as possible."
        return None

    # ------------------------------------------------------------ rules path

    async def _rules_reply(self, ctx: TurnContext, history: List[Dict[str, Any]], text: str) -> str:
        p = ctx.profile
        t = (text or "").lower().strip()
        intent = extract_intent(t)

        # The owner's own answers beat keyword routing - "is there parking?"
        # matches no intent but is exactly what their FAQ covers. Actions
        # (book/cancel/escalate) still win: "cancel my appointment" must not
        # be answered with the cancellation-policy FAQ.
        if intent not in (Intent.ESCALATE_TO_HUMAN, Intent.BOOK_APPOINTMENT,
                          Intent.RESCHEDULE_APPOINTMENT, Intent.CANCEL_APPOINTMENT):
            faq = _best_faq(p, t)
            if faq:
                return faq

        if intent == Intent.UNKNOWN:
            if any(c in t for c in COURTESIES):
                ctx.end_call = True
                return "You're very welcome. Thanks for calling, and have a lovely day."
            if any(g in t for g in GREETINGS):
                return (f"Hello, thanks for calling {p.name}. I can tell you about our services "
                        "and hours, or take a message for the team. What can I do for you?")
            # Two misunderstandings in a row: stop looping, get a person.
            misses = sum(1 for turn in history[-4:] if turn.get("speaker") == "agent"
                         and "didn't catch" in str(turn.get("text", "")))
            if misses >= 1:
                await dispatch(ctx, "transfer_to_human", {"reason": "could not understand the caller"})
                return self._reply_for_effects(ctx) or "Let me get someone from the team to help you."
            return "Sorry, I didn't catch that. Could you say it another way?"

        if intent == Intent.ESCALATE_TO_HUMAN:
            await dispatch(ctx, "transfer_to_human", {"reason": "caller asked for a person"})
            return self._reply_for_effects(ctx) or "Let me get someone from the team to help you."

        if intent in (Intent.BOOK_APPOINTMENT, Intent.RESCHEDULE_APPOINTMENT, Intent.CANCEL_APPOINTMENT):
            # Without a model we cannot reliably capture a date, time and name
            # from free speech, so do not pretend to: ask the team to follow up.
            what = {Intent.BOOK_APPOINTMENT: "booking", Intent.RESCHEDULE_APPOINTMENT: "reschedule",
                    Intent.CANCEL_APPOINTMENT: "cancellation"}[intent]
            await dispatch(ctx, "transfer_to_human", {"reason": f"{what} request: {text[:160]}"})
            return (self._reply_for_effects(ctx) if ctx.transfer_to else
                    f"I've noted your {what} request, and a member of the team will call you back "
                    "to confirm it. Is there anything else?")

        if intent == Intent.CHECK_AVAILABILITY or any(k in t for k in ("hours", "open", "when", "time")):
            return f"Our hours are {p.hours_summary()} Is there anything else I can help with?"

        if any(k in t for k in ("how much", "price", "cost", "charge", "fee")):
            priced = [s for s in p.services if s.price]
            if priced:
                items = ", ".join(f"{s.name} is {p.format_price(s.price)}" for s in priced[:3])
                return f"{items}. Would you like a team member to help you book?"
            return "I'll have a colleague confirm the price for you. Shall I take your number?"

        if p.about:
            return (f"{p.about.strip()[:220]} Is there something specific I can help with?")
        return "I'll have a colleague get back to you on that. Can I take your name and number?"


_agent = ReceptionistAgent()


def get_agent() -> ReceptionistAgent:
    return _agent
