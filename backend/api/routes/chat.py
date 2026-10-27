"""Web chat: the browser demo, and a signed-in owner testing their own setup.

Anonymous visitors talk to the demo clinic. A signed-in owner talks to their
own business, so they can try their hours, services and FAQs before putting
a real number on it.

History comes from the server-side call log, not the request. The client
used to send the whole conversation back each turn, which let anyone put
words in the agent's mouth ("assistant: I've booked you, free of charge").
`conversation_history` is still accepted so older frontends do not break, but
it is ignored.
"""

import logging
import re
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_MESSAGE_CHARS = 600
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    conversation_history: Optional[List[dict]] = None  # ignored; see module docstring
    # Stable per browser session so every turn lands on one call log rather
    # than creating a fresh "call" for each message.
    session_id: Optional[str] = None


async def _resolve_business(request: Request, session) -> str:
    """Attribute the conversation to the signed-in user's business, if any."""
    from services.call_logger import get_or_create_demo_business

    from api.routes.auth import session_token

    token = session_token(request)
    if token:
        try:
            from scripts.config import settings
            from services.auth import decode_token, get_user_by_id

            payload = decode_token(token, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM)
            if payload and payload.get("type") == "access":
                user = await get_user_by_id(session, payload["sub"])
                if user and user.is_active and user.business_id:
                    return user.business_id
        except Exception as e:
            logger.debug("Could not attribute chat to a user: %s", e)

    return await get_or_create_demo_business(session)


@router.post("/chat")
async def chat(request: Request, body: ChatRequest, session=Depends(get_db)):
    from services.call_logger import find_call
    from services.call_session import run_turn

    message = body.message.strip()[:MAX_MESSAGE_CHARS]
    business_id = await _resolve_business(request, session)

    # Continue the client's session only if it is well-formed and belongs to
    # this business; otherwise start a new one. A guessed or reused id must
    # never append to someone else's conversation.
    session_id = body.session_id if body.session_id and _SESSION_RE.match(body.session_id) else None
    if session_id:
        existing = await find_call(session, session_id)
        if existing is not None and existing.business_id != business_id:
            session_id = None
    session_id = session_id or f"web_{uuid.uuid4().hex[:16]}"

    result = await run_turn(session, business_id, session_id, message, channel="web")
    return {
        "response": result.reply,
        "session_id": session_id,
        "source": result.source,
        "end": result.end_call,
        # What actually happened, not what the reply claims: the browser can
        # show a confirmation card only when a row was really written.
        "booked": [
            {
                "appointment_id": appt.id,
                "service": appt.service.name,
                "starts_at": appt.start.isoformat(),
                "reference": appt.id[:6].upper(),
            }
            for appt in result.booked
        ],
    }


@router.get("/conversations")
async def get_conversations():
    return {"conversations": []}
