from typing import Optional, Dict, Any, List
from abc import ABC, abstractmethod


class STTProvider(ABC):
    """Abstract base class for Speech-to-Text providers."""

    @abstractmethod
    async def initialize(self):
        """Initialize the STT provider."""
        pass

    @abstractmethod
    async def cleanup(self):
        """Clean up the STT provider."""
        pass

    @abstractmethod
    async def transcribe(self, audio_data: bytes, session_id: str, is_final: bool = False) -> Dict[str, Any]:
        """Transcribe audio data.

        Args:
            audio_data: Raw audio bytes
            session_id: Session ID for the call
            is_final: Whether this is the final chunk of an utterance

        Returns:
            Dict with transcription results
        """
        pass

    async def transcribe_stream(self, audio_generator, session_id: str):
        """Stream transcribe audio chunks.

        Args:
            audio_generator: Async generator yielding audio chunks
            session_id: Session ID for the call

        Yields:
            Dict with transcription results
        """
        async for audio_chunk in audio_generator:
            yield await self.transcribe(audio_chunk, session_id)