"""Text-to-speech engines.

Two implementations, both reached through `POST /api/speech/tts`:

- `piper_tts`   free, offline, MIT. The demo and self-host engine.
- `sarvam_tts`  Hindi, Gujarati and Indian English voices. Paid, needs a key.

ElevenLabs is also supported, but it is a direct REST call in
`api/routes/speech.py` rather than a class here, because it needs no streaming
state. The route picks the engine (`_resolve_tts_provider`) and every engine
returns audio bytes, so adding a third means adding that function and a branch,
not a subclass.
"""
