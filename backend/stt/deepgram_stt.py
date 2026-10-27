"""Deepgram pre-recorded transcription for uploaded audio.

The browser demo posts a recorded clip to `POST /api/speech/stt?provider=deepgram`;
this is the client that answers it. It is deliberately a plain REST call rather
than the WebSocket scaffolding that used to live here: the WebSocket was never
connected to anything and its `transcribe()` returned the literal string
"placeholder transcription", which the endpoint silently turned into an empty
reply.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import aiohttp

from scripts.config import settings
from stt.__init__ import STTProvider

logger = logging.getLogger(__name__)

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


class DeepgramSTT(STTProvider):
    """One-shot transcription of an uploaded clip."""

    def __init__(self) -> None:
        self.api_key: Optional[str] = None
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        self.api_key = (settings.DEEPGRAM_API_KEY or "").strip()
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is not set")

    async def cleanup(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def transcribe(
        self,
        audio_data: bytes,
        session_id: str = "web",
        is_final: bool = True,
        language: Optional[str] = None,
        mimetype: str = "audio/webm",
    ) -> Dict[str, Any]:
        """Transcribe with Deepgram, returning their `transcript` key.

        Raises rather than returning empty text: a failed transcription must
        surface as an error the route can report, never as "the caller said
        nothing".
        """
        key = self.api_key or (settings.DEEPGRAM_API_KEY or "").strip()
        if not key:
            raise RuntimeError("DEEPGRAM_API_KEY is not set")
        if not audio_data:
            raise ValueError("empty audio")

        params = {
            "model": settings.DEEPGRAM_MODEL,
            "smart_format": "true",
            "punctuate": "true",
        }
        if language:
            params["language"] = language

        headers = {"Authorization": f"Token {key}", "Content-Type": mimetype}
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(DEEPGRAM_URL, params=params, data=audio_data,
                                    headers=headers) as response:
                body = await response.read()
                if response.status != 200:
                    detail = body[:200].decode("utf-8", "replace")
                    logger.warning("Deepgram failed %s: %s", response.status, detail)
                    raise RuntimeError(f"Deepgram returned {response.status}: {detail}")
                payload = await response.json(content_type=None)

        try:
            best = payload["results"]["channels"][0]["alternatives"][0]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected Deepgram response: {e}") from e

        text = (best.get("transcript") or "").strip()
        return {
            "transcript": text,
            # The upload route reads `text`; keep both keys so either contract
            # works for a caller.
            "text": text,
            "confidence": float(best.get("confidence") or 0.0),
            "language": language or "",
            "is_final": is_final,
        }