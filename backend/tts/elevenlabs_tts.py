import asyncio
import json
import base64
import logging
from typing import AsyncGenerator, Optional, Dict, Any
from abc import ABC, abstractmethod

from tts.__init__ import TTSProvider

logger = logging.getLogger(__name__)


class ElevenLabsTTS(TTSProvider):
    """ElevenLabs streaming TTS implementation."""
    
    def __init__(self):
        self.api_key: Optional[str] = None
        self.base_url: str = "https://api.elevenlabs.io/v1/text-to-speech"
        self.voice_id: Optional[str] = None
        self.is_streaming: bool = False
    
    async def initialize(self):
        """Initialize ElevenLabs connection."""
        from scripts.config import settings
        
        self.api_key = settings.ELEVENLABS_API_KEY
        self.voice_id = settings.ELEVENLABS_VOICE_ID
        
        if not self.api_key:
            raise ValueError("ElevenLabs API key is required")
        
        logger.info("ElevenLabs TTS initialized")
    
    async def cleanup(self):
        """Clean up ElevenLabs resources."""
        self.is_streaming = False
        logger.info("ElevenLabs TTS cleaned up")
    
    async def speak(self, text: str, session_id: str, is_interrupt: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """Convert text to speech using ElevenLabs streaming API.
        
        Args:
            text: Text to convert to speech
            session_id: Session ID for the call
            is_interrupt: Whether this is an interruption
            
        Yields:
            Dict with audio chunk or status information
        """
        if is_interrupt:
            yield {
                'audio_chunk': b'',  # Empty audio to stop current stream
                'is_final': True,
                'is_interrupt': True,
            }
            return
        
        try:
            # In a real implementation, we would call ElevenLabs streaming API
            # For now, return a placeholder implementation
            audio_chunk = b'placeholder_audio_chunk'
            
            yield {
                'audio_chunk': audio_chunk,
                'is_final': False,
                'is_interrupt': False,
            }
            
            # Simulate streaming delay
            await asyncio.sleep(0.01)
            
            yield {
                'audio_chunk': b'',
                'is_final': True,
                'is_interrupt': False,
            }
            
        except Exception as e:
            logger.error(f"Error generating speech with ElevenLabs: {e}")
            yield {
                'audio_chunk': None,
                'is_final': True,
                'error': str(e),
            }
