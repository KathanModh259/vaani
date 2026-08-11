"""Piper TTS - free, offline, MIT-licensed neural speech.

Runs entirely on CPU with no API key and no per-character billing, which makes
it the demo/self-host engine. Swap TTS_PROVIDER=elevenlabs once a paying client
needs the higher-end voices; the /api/tts contract does not change.

Voices are single .onnx files (plus a .onnx.json config beside them). Drop them
in backend/models/piper/ and they are picked up automatically:

    python -m piper.download_voices en_GB-cori-high --download-dir models/piper

Any number of voices can be present; callers select one by full name
("en_GB-cori-high") or by language prefix ("en", "hi"). Each is loaded lazily
on first use and then cached, so unused voices cost nothing.
"""

import asyncio
import io
import logging
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

VOICES_DIR = Path(__file__).resolve().parent.parent / "models" / "piper"

_voices = {}                 # name -> PiperVoice
_lock = threading.Lock()     # guards concurrent first-loads of the same voice
_import_error = None


def _piper_available():
    """Is the piper package importable?"""
    global _import_error
    try:
        import piper  # noqa: F401
        return True
    except ImportError as e:
        _import_error = f"piper-tts not installed (pip install piper-tts): {e}"
        return False


def _voice_files():
    """Every installed voice, as {name: path}. Explicit config wins."""
    from scripts.config import settings

    found = {}

    if settings.PIPER_VOICE:
        p = Path(settings.PIPER_VOICE).expanduser()
        if p.is_file():
            found[p.stem] = p

    if VOICES_DIR.is_dir():
        for p in sorted(VOICES_DIR.glob("*.onnx")):
            found.setdefault(p.stem, p)

    return found


def _resolve_name(requested):
    """Map a request to an installed voice name.

    Accepts a full name ("en_GB-cori-high"), a language prefix ("en", "hi"),
    or None for the default. Returns None when nothing matches.
    """
    files = _voice_files()
    if not files:
        return None

    names = list(files)

    if requested:
        if requested in files:
            return requested
        # Language prefix: "hi" matches "hi_IN-priyamvada-medium".
        low = requested.lower()
        for n in names:
            if n.lower().startswith(low):
                return n
        return None

    from scripts.config import settings
    if settings.PIPER_DEFAULT_VOICE and settings.PIPER_DEFAULT_VOICE in files:
        return settings.PIPER_DEFAULT_VOICE

    # Prefer an English voice as the default, else whatever is installed.
    for n in names:
        if n.lower().startswith("en"):
            return n
    return names[0]


def _get_voice(requested=None):
    """Load (and cache) the requested voice. Returns None when unavailable."""
    if not _piper_available():
        return None

    name = _resolve_name(requested)
    if name is None:
        return None

    if name in _voices:
        return _voices[name]

    with _lock:
        if name in _voices:            # another thread won the race
            return _voices[name]
        try:
            from piper import PiperVoice
            path = _voice_files()[name]
            _voices[name] = PiperVoice.load(str(path))
            logger.info("Piper loaded voice: %s", name)
        except Exception as e:
            logger.warning("Piper failed to load voice %s: %s", name, e)
            return None

    return _voices[name]


def list_voices():
    """Installed voice names, for the status endpoint."""
    return sorted(_voice_files()) if _piper_available() else []


def is_available() -> bool:
    return bool(list_voices())


def status() -> dict:
    if not _piper_available():
        return {"available": False, "detail": _import_error, "voices": []}

    voices = list_voices()
    if not voices:
        return {
            "available": False,
            "detail": f"no voice models in {VOICES_DIR} (see .env.example)",
            "voices": [],
        }
    return {
        "available": True,
        "detail": None,
        "voices": voices,
        "default": _resolve_name(None),
        "loaded": sorted(_voices),
    }


class EmptySynthesis(ValueError):
    """Text produced no phonemes (punctuation, emoji, or mojibake only)."""


def _chunk_bytes(chunk):
    """Raw int16 PCM out of a Piper AudioChunk, across API versions."""
    for attr in ("audio_int16_bytes", "audio_int16_array", "audio"):
        data = getattr(chunk, attr, None)
        if data is None:
            continue
        return data.tobytes() if hasattr(data, "tobytes") else data
    return chunk if isinstance(chunk, (bytes, bytearray)) else b""


def _synthesize_sync(text: str, requested) -> bytes:
    """Render text to a 16-bit PCM WAV. Runs off the event loop.

    The WAV header is written from the voice config rather than left to
    Piper's helper: when text phonemizes to nothing the helper never sets the
    format and `wave` then fails with a confusing "# channels not specified".
    Doing it explicitly turns that into a clear EmptySynthesis instead.
    """
    voice = _get_voice(requested)
    if voice is None:
        raise RuntimeError(_import_error or f"No Piper voice available for {requested!r}")

    sample_rate = getattr(voice.config, "sample_rate", 22050)

    pcm = bytearray()
    if hasattr(voice, "synthesize"):
        for chunk in voice.synthesize(text):
            pcm += _chunk_bytes(chunk)
    elif hasattr(voice, "synthesize_stream_raw"):
        for chunk in voice.synthesize_stream_raw(text):
            pcm += chunk
    else:  # pragma: no cover - very old piper
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            voice.synthesize_wav(text, wf)
        return buf.getvalue()

    if not pcm:
        raise EmptySynthesis(f"no speech produced for {text[:40]!r}")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(pcm))
    return buf.getvalue()


async def synthesize(text: str, voice=None) -> bytes:
    """Render text to WAV bytes without blocking the event loop."""
    return await asyncio.to_thread(_synthesize_sync, text, voice)


async def warmup():
    """Load the default voice ahead of the first request.

    A "high" quality model is ~114MB and takes a couple of seconds to load;
    doing it at startup keeps that cost off the first visitor.
    """
    if not is_available():
        return False
    return await asyncio.to_thread(lambda: _get_voice(None) is not None)
