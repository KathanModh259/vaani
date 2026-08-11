import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    conversation_history: Optional[List[dict]] = []
    # Stable per browser session so every turn lands on one call log rather
    # than creating a fresh "call" for each message.
    session_id: Optional[str] = None


async def _resolve_business(request: Request, session) -> str:
    """Attribute the conversation to the signed-in user's business, if any."""
    from services.call_logger import get_or_create_demo_business

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from scripts.config import settings
            from services.auth import decode_token, get_user_by_id

            payload = decode_token(auth[7:], settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM)
            if payload and payload.get("type") == "access":
                user = await get_user_by_id(session, payload["sub"])
                if user and user.business_id:
                    return user.business_id
        except Exception as e:
            logger.debug("Could not attribute chat to a user: %s", e)

    return await get_or_create_demo_business(session)


@router.post("/chat")
async def chat(request: Request, body: ChatRequest):
    from llm_agent.agent import get_llm_agent
    from services.call_logger import record_turn
    from services.conversation_manager import ConversationState

    db_manager = getattr(request.app.state, "db_manager", None)
    if not db_manager:
        return {"response": "Backend is starting up. Please try again in a moment."}

    session_id = body.session_id or f"web_{uuid.uuid4().hex[:12]}"
    agent = get_llm_agent(db_manager)

    session = await db_manager.get_session()
    try:
        business_id = await _resolve_business(request, session)

        conv = ConversationState(business_id, session_id)
        for turn in body.conversation_history:
            if turn.get("role") == "user":
                conv.add_turn("caller", turn["content"])
            else:
                conv.add_turn("agent", turn["content"])

        result = await agent.process_turn(conv, session, body.message)

        transcript = [
            {
                "speaker": t.get("speaker"),
                "text": t.get("text"),
                "timestamp": t["timestamp"].isoformat() if t.get("timestamp") else None,
            }
            for t in conv.turns
        ]
        intent = result.get("intent")
        await record_turn(
            session,
            session_id=session_id,
            business_id=business_id,
            transcript=transcript,
            intent_name=getattr(intent, "name", None) or (intent if isinstance(intent, str) else None),
            escalated=bool(result.get("escalated") or conv.escalation_requested),
        )

        return {
            "response": result["response"],
            "session_id": session_id,
            "source": result.get("source", "rules"),
        }
    finally:
        await session.close()


@router.get("/conversations")
async def get_conversations():
    return {"conversations": []}
