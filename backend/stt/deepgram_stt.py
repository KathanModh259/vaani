import asyncio
import json
import base64
import logging
from typing import AsyncGenerator, Optional, Dict, Any
import aiohttp

from stt.__init__ import STTProvider

logger = logging.getLogger(__name__)


class DeepgramSTT(STTProvider):
    """Deepgram streaming STT implementation."""
    
    def __init__(self):
        self.api_key: Optional[str] = None
        self.base_url: str = "https://api.deepgram.com/v1/listen"
        self.session: Optional[aiohttp.ClientSession] = None
        self.socket: Optional[aiohttp.ClientWebSocketResponse] = None
        self.is_connected: bool = False
    
    async def initialize(self):
        """Initialize Deepgram connection."""
        pass
    
    async def cleanup(self):
        """Clean up Deepgram resources."""
        if self.socket and not self.socket.closed:
            await self.socket.close()
            self.socket = None
        
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
        
        self.is_connected = False
    
    async def transcribe(self, audio_data: bytes, session_id: str, is_final: bool = False) -> Dict[str, Any]:
        """Transcribe audio data using Deepgram streaming API.
        
        Args:
            audio_data: Raw audio bytes (16kHz, 16-bit mono, PCM)
            session_id: Session ID for the call
            is_final: Whether this is the final chunk of an utterance
            
        Returns:
            Dict with transcription results
        """
        # For now, return a placeholder implementation
        # In a real implementation, we would use Deepgram's streaming API
        return {
            'text': 'placeholder transcription',
            'confidence': 0.85,
            'is_final': is_final,
            'error': None,
            'is_endpoint': False,
        }

    async def connect_websocket(self, dg_request: Dict[str, Any]):
        """Connect to Deepgram WebSocket for streaming transcription."""
        from scripts.config import settings
        
        if self.session is None:
            self.session = aiohttp.ClientSession()
        
        try:
            self.socket = await self.session.ws_connect(
                self.base_url,
                headers={
                    'Authorization': f'Bearer {settings.DEEPGRAM_API_KEY}',
                    'Content-Type': 'application/json',
                },
            )
            
            # Send request to start streaming
            await self.socket.send_json(dg_request)
            self.is_connected = True
            
            # Wait for ack
            if self.socket:
                response = await self.socket.receive()
                if response.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(response.data)
                    if data.get('type') != 'Results':
                        logger.warning(f"Deepgram connection issue: {data}")
                
        except Exception as e:
            logger.error(f"Error connecting to Deepgram WebSocket: {e}")
            await self.cleanup()
            raise
    
    async def send_audio_chunk(self, audio_data: bytes):
        """Send audio chunk to Deepgram WebSocket."""
        if not self.is_connected or not self.socket:
            raise RuntimeError("Deepgram WebSocket not connected")
        
        # Encode audio as base64
        encoded_audio = base64.b64encode(audio_data).decode('ascii')
        
        # Send audio chunk
        await self.socket.send_json({
            'event': 'audio_chunk',
            'data': encoded_audio,
        })
    
    async def close_stream(self):
        """Close the Deepgram stream."""
        if not self.is_connected or not self.socket:
            return
        
        # Send end-of-stream marker
        await self.socket.send_json({
            'event': 'close',
        })
        
        # Wait for response
        try:
            if self.socket:
                response = await self.socket.receive()
                if response.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(response.data)
                    logger.info(f"Deepgram stream closed: {data}")
        except Exception as e:
            logger.error(f"Error closing Deepgram stream: {e}")
        
        await self.cleanup()
