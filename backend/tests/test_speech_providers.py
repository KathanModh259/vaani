"""Speech providers that actually reach a network.

No live calls: the HTTP layer is faked, so these prove the request shape, the
error paths and - critically - that a failure is never reported to the caller
as "you said nothing".
"""

import json

import pytest

from scripts.config import settings
from stt import deepgram_stt


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}
        self._body = json.dumps(self._payload).encode()

    async def read(self):
        return self._body

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Captures the request instead of sending it."""

    last: dict = {}

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    def post(self, url, params=None, data=None, headers=None):
        FakeSession.last = {"url": url, "params": params, "data": data, "headers": headers}
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def close(self):
        pass


@pytest.fixture
def fake_deepgram(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setattr(settings, "DEEPGRAM_MODEL", "nova-2")

    def install(status=200, payload=None):
        payload = payload or {
            "results": {"channels": [{"alternatives": [
                {"transcript": "mujhe appointment chahiye", "confidence": 0.97}
            ]}]}
        }
        response = FakeResponse(status, payload)

        class Session(FakeSession):
            _response = response

        monkeypatch.setattr(deepgram_stt.aiohttp, "ClientSession", Session)
        return Session

    return install


async def test_deepgram_returns_the_transcript(fake_deepgram):
    fake_deepgram()
    engine = deepgram_stt.DeepgramSTT()
    await engine.initialize()
    result = await engine.transcribe(b"audio-bytes", language="hi", mimetype="audio/webm")

    assert result["transcript"] == "mujhe appointment chahiye"
    assert result["text"] == result["transcript"]      # the route reads either
    assert result["confidence"] == pytest.approx(0.97)

    sent = FakeSession.last
    assert sent["url"] == deepgram_stt.DEEPGRAM_URL
    assert sent["headers"]["Authorization"] == "Token dg-test"
    assert sent["headers"]["Content-Type"] == "audio/webm"
    assert sent["params"]["model"] == "nova-2"
    assert sent["params"]["language"] == "hi"


async def test_missing_key_fails_before_the_network(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "")
    engine = deepgram_stt.DeepgramSTT()
    with pytest.raises(RuntimeError, match="DEEPGRAM_API_KEY"):
        await engine.initialize()


async def test_a_provider_error_is_raised_not_swallowed(fake_deepgram):
    """The old implementation returned 'placeholder transcription' here."""
    fake_deepgram(status=401, payload={"err": "bad key"})
    engine = deepgram_stt.DeepgramSTT()
    await engine.initialize()
    with pytest.raises(RuntimeError, match="401"):
        await engine.transcribe(b"audio-bytes")


async def test_an_empty_result_is_an_empty_string_not_a_crash(fake_deepgram):
    fake_deepgram(payload={"results": {"channels": [{"alternatives": [
        {"transcript": "", "confidence": 0.0}]}]}})
    engine = deepgram_stt.DeepgramSTT()
    await engine.initialize()
    result = await engine.transcribe(b"audio-bytes")
    assert result["transcript"] == "" and result["text"] == ""


async def test_a_malformed_response_raises(fake_deepgram):
    fake_deepgram(payload={"unexpected": True})
    engine = deepgram_stt.DeepgramSTT()
    await engine.initialize()
    with pytest.raises(RuntimeError, match="Unexpected Deepgram response"):
        await engine.transcribe(b"audio-bytes")


async def test_empty_audio_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "dg-test")
    engine = deepgram_stt.DeepgramSTT()
    await engine.initialize()
    with pytest.raises(ValueError, match="empty audio"):
        await engine.transcribe(b"")