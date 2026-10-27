"""Low-latency phone conversations over Twilio ConversationRelay.

The <Gather>/<Say> loop in voice.py costs several round trips per reply:
Twilio finishes transcribing, calls us, waits for the whole LLM turn, then
starts a fresh TTS. ConversationRelay keeps one WebSocket open for the call:
Twilio streams speech recognition and speaks our text as it arrives, and the
caller can interrupt ("barge in").

    /api/voice/incoming (VOICE_MODE=relay)
        -> <Connect action="/api/voice/relay-ended"><ConversationRelay url="wss://.../api/voice/relay">
    wss /api/voice/relay      setup / prompt / interrupt  <->  text / end / language
    /api/voice/relay-ended    session over: <Dial> a human, hang up, or fall back to <Gather>

Turns still go through services.call_session.run_turn - same memory, tools,
alerts and transcripts as the <Gather> path. If a turn takes longer than
FILLER_AFTER, the caller hears "One moment." instead of dead air.
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable, Dict, Optional
from xml.sax.saxutils import quoteattr

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from scripts.config import settings
from services.language import script_language

logger = logging.getLogger(__name__)
router = APIRouter()

FILLER_AFTER = 0.9  # seconds of silence before "One moment."
FILLERS = {"en-IN": "One moment.", "hi-IN": "एक पल।", "gu-IN": "એક ક્ષણ."}
MAX_CALLER_TURNS = 40

# Google voices ConversationRelay can speak, per language.
RELAY_VOICES = {"en-IN": "en-IN-Standard-A", "hi-IN": "hi-IN-Standard-A", "gu-IN": "gu-IN-Standard-A"}


def relay_enabled() -> bool:
    return settings.VOICE_MODE == "relay" and settings.PUBLIC_BASE_URL.startswith("https://")


def relay_twiml(profile, greeting: str, bid_param: Optional[str] = None) -> str:
    ws_url = settings.PUBLIC_BASE_URL.replace("https://", "wss://", 1) + "/api/voice/relay"
    lang = profile.language or "en-IN"
    action = "/api/voice/relay-ended" + (f"?bid={bid_param}" if bid_param else "")
    return (
        f'<Connect action={quoteattr(action)}>'
        f'<ConversationRelay url={quoteattr(ws_url)} welcomeGreeting={quoteattr(greeting)} '
        f'language={quoteattr(lang)} ttsProvider={quoteattr(settings.RELAY_TTS_PROVIDER)} '
        f'voice={quoteattr(RELAY_VOICES.get(lang, RELAY_VOICES["en-IN"]))} '
        f'transcriptionProvider={quoteattr(settings.RELAY_TRANSCRIPTION_PROVIDER)} '
        f'interruptible="any" dtmfDetection="false">'
        f'<Parameter name="business_id" value={quoteattr(profile.id)}/>'
        f'</ConversationRelay></Connect>'
    )


Send = Callable[[Dict], Awaitable[None]]


class RelaySession:
    """One call's WebSocket conversation. Transport-free so it can be tested
    by feeding it messages directly."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.call_sid: Optional[str] = None
        self.business_id: Optional[str] = None
        self.caller: Optional[str] = None
        self.language = "en-IN"
        self.default_language = "en-IN"
        self.turns = 0
        self.ended = False

    async def handle(self, msg: Dict, send: Send) -> None:
        kind = msg.get("type")
        if kind == "setup":
            from api.routes.voice import _caller
            self.call_sid = msg.get("callSid")
            self.business_id = (msg.get("customParameters") or {}).get("business_id")
            self.caller = _caller(msg.get("from"))
            await self._load_language()
        elif kind == "prompt" and msg.get("last", True) and not self.ended:
            text = (msg.get("voicePrompt") or "").strip()
            if text:
                await self._turn(text, send)
        elif kind == "interrupt":
            logger.debug("Caller interrupted on %s", self.call_sid)
        elif kind == "error":
            logger.warning("ConversationRelay error on %s: %s", self.call_sid, msg.get("description"))

    async def _load_language(self):
        from services.business_profile import load_profile
        session = await self.session_factory()
        try:
            profile = await load_profile(session, self.business_id) if self.business_id else None
            if profile:
                self.default_language = self.language = profile.language or "en-IN"
        finally:
            await session.close()

    async def _end(self, send: Send, **handoff) -> None:
        self.ended = True
        await send({"type": "end", "handoffData": json.dumps(handoff)})

    async def _turn(self, text: str, send: Send) -> None:
        from services.call_session import run_turn

        self.turns += 1
        if self.turns > MAX_CALLER_TURNS:
            await self._end(send, action="callback", reason="call ran long")
            return

        session = await self.session_factory()
        try:
            task = asyncio.create_task(run_turn(session, self.business_id, self.call_sid, text,
                                                channel="phone", caller_phone=self.caller))
            done, _ = await asyncio.wait({task}, timeout=FILLER_AFTER)
            if not done:
                # Tools (availability, booking) are running; say something.
                await send({"type": "text", "token": FILLERS.get(self.language, FILLERS["en-IN"]), "last": True})
            try:
                result = await task
            except Exception as e:
                logger.exception("Relay turn failed for %s: %s", self.call_sid, e)
                await send({"type": "text", "token": "Sorry, I'm having trouble right now. "
                            "Please call back in a few minutes.", "last": True})
                await self._end(send, action="hangup")
                return
        finally:
            await session.close()

        # Follow the language the reply is written in for both TTS and the
        # recogniser listening for the caller's next sentence.
        lang = script_language(result.reply) or self.default_language
        if lang != self.language:
            self.language = lang
            await send({"type": "language", "ttsLanguage": lang, "transcriptionLanguage": lang})
        await send({"type": "text", "token": result.reply, "last": True})

        if result.transfer_to:
            await self._end(send, action="transfer", to=result.transfer_to)
        elif result.end_call:
            await self._end(send, action="hangup")


def _valid_ws_signature(websocket: WebSocket) -> bool:
    """Twilio signs the WebSocket upgrade over the wss:// URL, with no body."""
    if not settings.TWILIO_VALIDATE_SIGNATURE:
        return True
    if not settings.TWILIO_AUTH_TOKEN:
        return settings.ENVIRONMENT.lower() not in ("production", "prod")
    from twilio.request_validator import RequestValidator

    url = settings.PUBLIC_BASE_URL.replace("https://", "wss://", 1) + websocket.url.path
    if websocket.url.query:
        url += "?" + websocket.url.query
    return RequestValidator(settings.TWILIO_AUTH_TOKEN).validate(
        url, {}, websocket.headers.get("x-twilio-signature", ""))


@router.websocket("/relay")
async def relay_socket(websocket: WebSocket):
    if not _valid_ws_signature(websocket):
        logger.warning("Rejected ConversationRelay socket with bad signature")
        await websocket.close(code=1008)
        return
    await websocket.accept()

    from db import get_manager

    async def new_session():
        return await (await get_manager()).get_session()

    relay = RelaySession(new_session)

    async def send(payload: Dict) -> None:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await relay.handle(msg, send)
    except WebSocketDisconnect:
        pass
