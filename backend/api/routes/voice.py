"""Twilio voice webhooks.

Twilio expects TwiML (XML) back from these endpoints, not JSON - returning
JSON makes the carrier drop the call. The flow is:

    /incoming  -> greet, then <Gather input="speech"> the caller's reply
    /respond   -> run the transcript through the agent, speak the answer,
                  and gather again until the caller hangs up

Point your Twilio number's "A call comes in" webhook at
POST https://yourdomain.com/api/voice/incoming
"""

import logging
import uuid
from xml.sax.saxutils import escape

from fastapi import APIRouter, Form, Request, Response

logger = logging.getLogger(__name__)
router = APIRouter()

GREETING = "Thank you for calling. This is Vaani. How can I help you today?"
FALLBACK = "Sorry, I did not catch that. Could you say it once more?"
GOODBYE = "Thanks for calling. Goodbye."

# Twilio's own neural voice - no TTS credits needed for the phone path.
TWILIO_VOICE = "Polly.Aditi"


def _twiml(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        media_type="application/xml",
    )


def _gather(prompt: str, action: str = "/api/voice/respond") -> str:
    """Speak a prompt and listen for the caller's reply."""
    return (
        f'<Gather input="speech" action="{action}" method="POST" '
        f'speechTimeout="auto" language="en-IN" actionOnEmptyResult="true">'
        f'<Say voice="{TWILIO_VOICE}">{escape(prompt)}</Say>'
        f"</Gather>"
    )


@router.get("/status")
async def voice_status():
    from scripts.config import settings

    return {
        "status": "voice service running",
        "twilio_configured": bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN),
        "phone_number": settings.TWILIO_PHONE_NUMBER or None,
        "webhook": "/api/voice/incoming",
    }


@router.post("/incoming")
async def handle_incoming_call(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
):
    """First leg of a call: greet and start listening."""
    logger.info("Incoming call %s from %s", CallSid or "?", From or "unknown")
    return _twiml(_gather(GREETING) + f'<Say voice="{TWILIO_VOICE}">{escape(GOODBYE)}</Say>')


@router.post("/respond")
async def respond(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    SpeechResult: str = Form(default=""),
):
    """Caller said something: answer it and keep the conversation going."""
    said = (SpeechResult or "").strip()
    if not said:
        return _twiml(_gather(FALLBACK))

    session_id = CallSid or f"call_{uuid.uuid4().hex[:12]}"
    reply = FALLBACK

    db_manager = getattr(request.app.state, "db_manager", None)
    if db_manager:
        session = await db_manager.get_session()
        try:
            from llm_agent.agent import get_llm_agent
            from services.call_logger import get_or_create_demo_business, record_turn
            from services.conversation_manager import ConversationState

            business_id = await get_or_create_demo_business(session)
            conv = ConversationState(business_id, session_id)
            result = await get_llm_agent(db_manager).process_turn(conv, session, said)
            reply = result.get("response") or FALLBACK

            await record_turn(
                session,
                session_id=session_id,
                business_id=business_id,
                transcript=[
                    {"speaker": t.get("speaker"), "text": t.get("text")}
                    for t in conv.turns
                ],
                intent_name=result.get("intent"),
                escalated=bool(result.get("escalated") or conv.escalation_requested),
                customer_phone=From or None,
            )
        except Exception as e:
            logger.error("Voice turn failed for %s: %s", session_id, e)
            reply = "Sorry, I am having trouble right now. Let me put you through to someone."
        finally:
            await session.close()

    return _twiml(_gather(reply) + f'<Say voice="{TWILIO_VOICE}">{escape(GOODBYE)}</Say>')
