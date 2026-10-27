"""Sarvam AI text-to-speech: voices built for Indian languages.

Piper has no Gujarati voice and its Hindi is serviceable at best; ElevenLabs
is priced for English-first markets. Sarvam's Bulbul models speak Hindi,
Gujarati and Indian English natively, which is who Vaani's callers are.

API: POST https://api.sarvam.ai/text-to-speech (header api-subscription-key),
returns {"audios": [<base64 WAV>]}. The language is picked per request from
the text's script, so one reply in Hindi and the next in English each get the
right voice.
"""

import base64
import logging
from typing import Optional

import aiohttp

from scripts.config import settings
from services.language import script_language

logger = logging.getLogger(__name__)

URL = "https://api.sarvam.ai/text-to-speech"
MAX_CHARS = 1500  # the API rejects longer inputs


class SarvamError(Exception):
    pass


def is_available() -> bool:
    return bool(settings.SARVAM_API_KEY)


async def synthesize(text: str, language: Optional[str] = None, speaker: Optional[str] = None) -> bytes:
    lang = language or script_language(text) or "en-IN"
    payload = {
        "text": text[:MAX_CHARS],
        "language_code": lang,
        "speaker": speaker or settings.SARVAM_SPEAKER,
        "model": settings.SARVAM_MODEL,
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.post(URL, json=payload, headers={"api-subscription-key": settings.SARVAM_API_KEY}) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise SarvamError(f"Sarvam TTS {r.status}: {str(body)[:200]}")
    audios = body.get("audios") or []
    if not audios:
        raise SarvamError("Sarvam TTS returned no audio")
    return base64.b64decode(audios[0])
