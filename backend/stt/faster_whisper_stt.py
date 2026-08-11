"""faster-whisper STT - free, offline, MIT-licensed transcription.

CTranslate2 reimplementation of OpenAI Whisper: no API key, no per-minute
billing, and the "base" model transcribes a short utterance faster than
real time on a plain CPU. Used for the demo and self-hosted deploys; set
STT_PROVIDER=deepgram for the lower-latency paid path on client calls.

The model downloads from HuggingFace on first use (~75 MB for "base") and is
cached under ~/.cache/huggingface thereafter.
"""

import asyncio
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_model = None
_load_attempted = False
_load_error = None


def _load_model():
    """Load the Whisper model once. Returns None when unavailable."""
    global _model, _load_attempted, _load_error

    if _load_attempted:
        return _model
    _load_attempted = True

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        _load_error = "faster-whisper not installed (pip install faster-whisper)"
        logger.info("faster-whisper unavailable: %s", _load_error)
        return None

    from scripts.config import settings

    try:
        _model = WhisperModel(
            settings.WHISPER_MODEL_SIZE,
            device=settings.WHISPER_DEVICE,
            compute_type=settings.WHISPER_COMPUTE_TYPE,
        )
        logger.info(
            "faster-whisper loaded model=%s device=%s",
            settings.WHISPER_MODEL_SIZE,
            settings.WHISPER_DEVICE,
        )
    except Exception as e:
        _load_error = f"failed to load model: {e}"
        logger.warning("faster-whisper unavailable: %s", _load_error)
        _model = None

    return _model


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        global _load_error
        _load_error = "faster-whisper not installed (pip install faster-whisper)"
        return False
    return True


def status() -> dict:
    available = is_available()
    return {
        "available": available,
        "detail": None if available else _load_error,
    }


def _transcribe_sync(audio_bytes: bytes, suffix: str, language):
    model = _load_model()
    if model is None:
        raise RuntimeError(_load_error or "Whisper model unavailable")

    # faster-whisper reads from a path; delete_on_close semantics differ across
    # platforms, so write and close before handing the path over.
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio_bytes)
        segments, info = model.transcribe(
            path,
            language=language,
            beam_size=1,
            vad_filter=True,
        )
        text = "".join(seg.text for seg in segments).strip()
        return {
            "text": text,
            "language": getattr(info, "language", language),
            "duration": getattr(info, "duration", None),
        }
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def transcribe(audio_bytes: bytes, suffix: str = ".webm", language=None) -> dict:
    """Transcribe audio bytes without blocking the event loop."""
    return await asyncio.to_thread(_transcribe_sync, audio_bytes, suffix, language)
