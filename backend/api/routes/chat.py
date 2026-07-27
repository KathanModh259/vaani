import logging
from typing import List, Optional
from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    conversation_history: Optional[List[dict]] = []


@router.post("/chat")
async def chat(request: Request, body: ChatRequest):
    from llm_agent.agent import get_llm_agent
    from services.conversation_manager import ConversationState
    from db import DatabaseManager

    db_manager = getattr(request.app.state, 'db_manager', None)
    if not db_manager:
        return {"response": "Backend is starting up. Please try again in a moment."}

    agent = get_llm_agent(db_manager)
    conv = ConversationState("default", "web_" + str(hash(body.message)))
    for turn in body.conversation_history:
        if turn.get("role") == "user":
            conv.add_turn("caller", turn["content"])
        else:
            conv.add_turn("agent", turn["content"])

    session = await db_manager.get_session()
    try:
        result = await agent.process_turn(conv, session, body.message)
        return {"response": result["response"]}
    finally:
        await session.close()


@router.get("/conversations")
async def get_conversations():
    return {"conversations": []}
