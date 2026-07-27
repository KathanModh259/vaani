import logging
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status")
async def voice_status():
    return {"status": "voice service running"}


@router.post("/incoming")
async def handle_incoming_call():
    return {"status": "call handled"}
