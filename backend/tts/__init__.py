import asyncio
import json
import base64
import logging
from typing import AsyncGenerator, Optional, Dict, Any, List
from abc import ABC, abstractmethod
from enum import Enum

logger = logging.getLogger(__name__)


class TTSProvider(ABC):
    """Abstract base class for Text-to-Speech providers."""
    
    @abstractmethod
    async def initialize(self):
        """Initialize the TTS provider."""
        pass
    
    @abstractmethod
    async def cleanup(self):
        """Clean up the TTS provider."""
        pass
    
    @abstractmethod
    async def speak(self, text: str, session_id: str, is_interrupt: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """Convert text to speech with streaming support.
        
        Args:
            text: Text to convert to speech
            session_id: Session ID for the call
            is_interrupt: Whether this is an interruption
            
        Yields:
            Dict with audio chunk or status information
        """
        pass


class TTSInterface:
    """Interface class to manage multiple TTS providers with fallback."""
    
    def __init__(self):
        self.elevenlabs_provider: Optional[TTSProvider] = None
        self.azure_provider: Optional[TTSProvider] = None
        self.default_provider: Optional[TTSProvider] = None
        self.current_provider: Optional[TTSProvider] = None
        self.provider_error_counts: Dict[str, int] = {"elevenlabs": 0, "azure": 0}
    
    def register_provider(self, provider: TTSProvider, provider_name: str = "elevenlabs", is_default: bool = False):
        """Register a TTS provider.
        
        Args:
            provider: TTS provider instance
            provider_name: Name of the provider ("elevenlabs" or "azure")
            is_default: Whether this is the default provider
        """
        if provider_name == "elevenlabs":
            self.elevenlabs_provider = provider
        elif provider_name == "azure":
            self.azure_provider = provider
        
        if is_default:
            self.default_provider = provider
            self.current_provider = provider
    
    async def initialize(self):
        """Initialize all registered providers."""
        if self.elevenlabs_provider:
            await self.elevenlabs_provider.initialize()
        if self.azure_provider:
            await self.azure_provider.initialize()
        if self.current_provider is None:
            self.current_provider = self.default_provider
    
    async def cleanup(self):
        """Clean up all providers."""
        if self.elevenlabs_provider:
            await self.elevenlabs_provider.cleanup()
        if self.azure_provider:
            await self.azure_provider.cleanup()
    
    async def speak(self, text: str, session_id: str, is_interrupt: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """Convert text to speech using the current provider, with fallback.
        
        Args:
            text: Text to convert to speech
            session_id: Session ID for the call
            is_interrupt: Whether this is an interruption
            
        Yields:
            Dict with audio chunk or status information
        """
        provider = self.current_provider
        
        if provider is None:
            yield {
                'audio_chunk': None,
                'is_final': True,
                'error': 'No TTS provider available',
            }
            return
        
        try:
            async for result in provider.speak(text, session_id, is_interrupt):
                yield result
                
        except Exception as e:
            logger.error(f"Error speaking with {provider}: {e}")
            
            # Switch to fallback provider
            await self._switch_provider(provider)
            
            # Try fallback
            async for result in self.current_provider.speak(text, session_id, is_interrupt):
                yield result
    
    async def _switch_provider(self, failed_provider):
        """Switch to a different provider if the current one fails.
        
        Args:
            failed_provider: The failed provider instance
        """
        logger.warning(f"Switching from {type(failed_provider).__name__} to fallback provider")
        
        # Try Azure if currently using ElevenLabs, or vice versa
        if failed_provider is self.elevenlabs_provider and self.azure_provider:
            self.current_provider = self.azure_provider
        elif failed_provider is self.azure_provider and self.elevenlabs_provider:
            self.current_provider = self.elevenlabs_provider
        elif self.default_provider:
            self.current_provider = self.default_provider
        else:
            logger.error("No fallback provider available")
