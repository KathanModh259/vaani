"""Speech endpoints: server-side TTS and STT with automatic provider selection.

Keeping synthesis on the server means the ElevenLabs key lives in the backend
environment instead of the browser, and lets the free offline engines (Piper,
faster-whisper) serve the public demo at zero marginal cost. When no engine is
configured the endpoints return 503 with a reason, and the frontend falls back
to the browser's built-in Web Speech APIs so the demo still talks.
"""

import logging
from typing import Optional

import aiohttp
from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from scripts.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["speech"])

# Cap synthesis length so a public demo cannot be used to burn TTS credits.
MAX_TTS_CHARS = 1200
MAX_STT_BYTES = 10 * 1024 * 1024


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    voice: Optional[str] = None


def _resolve_tts_provider() -> str:
    """Pick the TTS engine: explicit config wins, else best available."""
    choice = settings.TTS_PROVIDER
    if choice and choice != "auto":
        return choice

    if settings.ELEVENLABS_API_KEY:
        return "elevenlabs"

    from tts import piper_tts
    if piper_tts.is_available():
        return "piper"

    return "none"


def _resolve_stt_provider() -> str:
    """Pick the STT engine: explicit config wins, else best available."""
    choice = settings.STT_PROVIDER
    if choice and choice != "auto":
        return choice

    if settings.DEEPGRAM_API_KEY:
        return "deepgram"

    from stt import faster_whisper_stt
    if faster_whisper_stt.is_available():
        return "faster_whisper"

    return "none"


@router.get("/speech/status")
async def speech_status():
    """Which engines are live. The frontend uses this to decide its fallback."""
    from stt import faster_whisper_stt
    from tts import piper_tts

    return {
        "tts": {
            "provider": _resolve_tts_provider(),
            "configured": settings.TTS_PROVIDER,
            "elevenlabs": {"available": bool(settings.ELEVENLABS_API_KEY)},
            "piper": piper_tts.status(),
        },
        "stt": {
            "provider": _resolve_stt_provider(),
            "configured": settings.STT_PROVIDER,
            "deepgram": {"available": bool(settings.DEEPGRAM_API_KEY)},
            "faster_whisper": faster_whisper_stt.status(),
        },
    }


async def _synthesize_elevenlabs(text: str, voice: Optional[str]) -> Response:
    voice_id = voice or settings.ELEVENLABS_VOICE_ID
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        "?output_format=mp3_44100_128"
    )
    payload = {
        "text": text,
        "model_id": settings.ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }
    headers = {
        "xi-api-key": settings.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            body = await resp.read()
            if resp.status != 200:
                detail = body[:200].decode("utf-8", "replace")
                logger.warning("ElevenLabs TTS failed %s: %s", resp.status, detail)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"TTS provider error ({resp.status})",
                )

    return Response(content=body, media_type="audio/mpeg")


async def _synthesize_piper(text: str, voice: Optional[str]) -> Response:
    from tts import piper_tts

    try:
        audio = await piper_tts.synthesize(text, voice=voice)
    except piper_tts.EmptySynthesis:
        # Nothing speakable in the input - a client bug, not a server fault.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Text contains nothing that can be spoken",
        )
    except Exception as e:
        logger.warning("Piper TTS failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Local TTS engine failed",
        )
    # Browsers cache by URL, and this is a POST, so tell them not to try.
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/tts")
async def text_to_speech(body: TTSRequest):
    """Synthesize speech. Returns audio bytes the browser can decode directly."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS]

    provider = _resolve_tts_provider()

    if provider == "elevenlabs":
        if not settings.ELEVENLABS_API_KEY:
            raise HTTPException(status_code=503, detail="ElevenLabs key not configured")
        return await _synthesize_elevenlabs(text, body.voice)

    if provider == "piper":
        from tts import piper_tts

        if body.voice and piper_tts._resolve_name(body.voice) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown voice {body.voice!r}. Installed: {piper_tts.list_voices()}",
            )
        return await _synthesize_piper(text, body.voice)

    # Nothing installed - the client falls back to browser speech synthesis.
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="No server-side TTS engine configured; use browser speech",
    )


@router.post("/stt")
async def speech_to_text(
    audio: UploadFile = File(...),
    language: Optional[str] = Form(None),
):
    """Transcribe an uploaded audio clip."""
    provider = _resolve_stt_provider()

    if provider == "none":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No server-side STT engine configured; use browser speech recognition",
        )

    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio upload")
    if len(data) > MAX_STT_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")

    if provider == "faster_whisper":
        from stt import faster_whisper_stt

        suffix = "." + (audio.filename or "clip.webm").rsplit(".", 1)[-1]
        try:
            result = await faster_whisper_stt.transcribe(data, suffix=suffix, language=language)
        except Exception as e:
            logger.warning("faster-whisper transcription failed: %s", e)
            raise HTTPException(status_code=502, detail="Transcription failed")
        return result

    if provider == "deepgram":
        from stt.deepgram_stt import DeepgramSTT

        engine = DeepgramSTT()
        await engine.initialize()
        try:
            result = await engine.transcribe(data, session_id="web", is_final=True)
        except Exception as e:
            logger.warning("Deepgram transcription failed: %s", e)
            raise HTTPException(status_code=502, detail="Transcription failed")
        finally:
            await engine.cleanup()
        return {"text": result.get("transcript", ""), "language": language}

    raise HTTPException(status_code=503, detail=f"Unknown STT provider: {provider}")
