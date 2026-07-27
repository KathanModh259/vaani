import asyncio
import json
import base64
import logging
from typing import Dict, Any, Optional, Callable, List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    from webrtcvad import Vad
    HAS_VAD = True
except ImportError:
    Vad = None
    HAS_VAD = False
    logger.warning("webrtcvad not installed — VAD disabled")

from services.conversation_manager import ConversationManager, ConversationState


class WebRTCCodec:
    """Utility class for encoding/decoding WebRTC PCM16 audio."""
    
    @staticmethod
    def encode_audio(audio_data: bytes) -> bytes:
        """Encode audio data as WebRTC PCM16."""
        return audio_data
    
    @staticmethod
    def decode_audio(audio_data: bytes) -> bytes:
        """Decode WebRTC PCM16 audio data."""
        return audio_data


class AudioBuffer:
    """Buffer for handling incoming audio chunks."""
    
    def __init__(self, max_size_bytes: int = 2048000):
        self.max_size_bytes = max_size_bytes
        self.buffer = bytearray()
        self.last_activity = datetime.now(timezone.utc)
        self.silence_detector = Vad(1)  # Aggressiveness level 1
        self.min_samples_silence = 3000  # 0.75 seconds at 16kHz
    
    def add_chunk(self, chunk: bytes) -> Dict[str, Any]:
        """Add audio chunk to buffer.
        
        Returns:
            Dict with keys:
            - 'chunk_count': number of chunks in buffer
            - 'is_silence': whether detected as silence
            - 'silence_timeout': time until silence timeout
            - 'clear': whether buffer is empty
        """
        self.buffer.extend(chunk)
        self.last_activity = datetime.now(timezone.utc)
        
        # Keep buffer size manageable
        if len(self.buffer) > self.max_size_bytes:
            self.buffer = self.buffer[-self.max_size_bytes:]
        
        # Check for silence
        is_silence = False
        if len(self.buffer) >= self.min_samples_silence:
            audio_bytes = bytes(self.buffer[:self.min_samples_silence])
            is_silence = self.silence_detector.is_active(audio_bytes)
        
        result = {
            'chunk_count': len(self.buffer) // 128,
            'is_silence': is_silence,
            'silence_timeout': 5.0,
            'clear': len(self.buffer) == 0,
        }
        
        return result
    
    def get_chunks(self) -> List[bytes]:
        """Get audio chunks from buffer."""
        # Split buffer into 20ms chunks
        chunk_size = 320  # 320 bytes = 20ms at 16kHz 16-bit mono
        return [self.buffer[i:i + chunk_size] for i in range(0, len(self.buffer), chunk_size)]
    
    def __bool__(self):
        return len(self.buffer) > 0

    def clear(self):
        """Clear buffer."""
        self.buffer.clear()
        self.last_activity = datetime.now(timezone.utc)


class MediaStream:
    """WebSocket media stream handler for Twilio Media Streams."""
    
    def __init__(self, conversation_manager: ConversationManager):
        self.conversation_manager = conversation_manager
        self.audio_buffer = AudioBuffer()
        self.vad_timeout_seconds = 1.5
        self.vad_active = False
        self.stream_start_time = None
        self.call_sid = None
        self.business_id = None
        self.stream_closed = False
        
        # Call event callbacks
        self.on_call_started: Optional[Callable] = None
        self.on_call_ended: Optional[Callable] = None
        self.on_transcription: Optional[Callable] = None
        self.on_agent_response: Optional[Callable] = None
    
    async def handle_stream(self, websocket):
        """Handle incoming media stream from Twilio."""
        try:
            # Initialize call
            await self.initialize_call()
            
            # Process incoming messages
            async for message in websocket:
                if self.stream_closed:
                    break
                
                await self.process_message(message)
                
        except Exception as e:
            logger.error(f"Error in media stream: {e}")
            await self.cleanup()
            
            if self.on_call_ended:
                await self.on_call_ended({
                    'reason': 'error',
                    'error': str(e),
                })
        
        await self.cleanup()
    
    async def process_message(self, message: str):
        """Process incoming WebSocket message."""
        try:
            data = json.loads(message)
            event = data.get('event')
            
            if event == 'start':
                await self.handle_start_event(data)
            elif event == 'media':
                await self.handle_media_event(data)
            elif event == 'stop':
                await self.handle_stop_event(data)
            elif event == 'mark':
                await self.handle_mark_event(data)
            elif event == 'connected':
                await self.handle_connected_event(data)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON message: {message}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    async def handle_start_event(self, data: Dict[str, Any]):
        """Handle stream start event."""
        logger.info("Media stream started")
        self.stream_start_time = datetime.now(timezone.utc)
    
    async def handle_connected_event(self, data: Dict[str, Any]):
        """Handle stream connected event."""
        logger.info("Media stream connected")
        if self.on_call_started:
            await self.on_call_started()
    
    async def handle_media_event(self, data: Dict[str, Any]):
        """Handle media event (audio chunk)."""
        audio_payload = data.get('media', {}).get('payload', '')
        
        # Decode base64 payload
        try:
            audio_chunk = base64.b64decode(audio_payload)
        except:
            logger.error("Failed to decode audio payload")
            return
        
        # Add to buffer
        buffer_status = self.audio_buffer.add_chunk(audio_chunk)
        
        # Check for VAD (Voice Activity Detection)
        if buffer_status['is_silence']:
            self.vad_active = False
        else:
            self.vad_active = True
        
        # Send audio chunk to STT for processing
        if self.vad_active:
            if self.on_transcription:
                await self.on_transcription(audio_chunk, self.call_sid)
    
    async def handle_stop_event(self, data: Dict[str, Any]):
        """Handle stream stop event."""
        logger.info("Media stream stopped")
        
        # Process remaining audio in buffer
        remaining_chunks = self.audio_buffer.get_chunks()
        if remaining_chunks:
            if self.on_transcription:
                for chunk in remaining_chunks:
                    await asyncio.sleep(0.01)  # Small delay between chunks
                    await self.on_transcription(chunk, self.call_sid)
        
        # Signal call end
        if self.on_call_ended:
            await self.on_call_ended({
                'reason': 'complete',
                'silence_detected': True,
                'chunks_processed': len(remaining_chunks),
            })
    
    async def handle_mark_event(self, data: Dict[str, Any]):
        """Handle mark event."""
        pass
    
    async def initialize_call(self):
        """Initialize a new call."""
        # In a real implementation, we would get call details from the request
        # For now, we'll use placeholder values
        self.call_sid = f"call_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        self.business_id = "test_business"
        
        # Start conversation
        conversation = self.conversation_manager.start_conversation(self.business_id, self.call_sid)
        conversation.add_turn('agent', "Hello, you've reached our AI receptionist. How can I help you today?")
        
        if self.on_call_started:
            await self.on_call_started()
    
    async def send_audio_response(self, audio_data: bytes):
        """Send audio response to Twilio."""
        if self.stream_closed:
            return
        
        try:
            encoded_audio = base64.b64encode(audio_data).decode('ascii')
            response = {
                'event': 'media',
                'media': {
                    'payload': encoded_audio,
                },
            }
            # In a real implementation, we would send this over WebSocket
            pass
        except Exception as e:
            logger.error(f"Error sending audio response: {e}")
    
    async def cleanup(self):
        """Clean up stream resources."""
        self.stream_closed = True
        self.audio_buffer.clear()
        
        # End conversation
        if self.call_sid:
            conversation = self.conversation_manager.get_conversation(self.call_sid)
            if conversation:
                conversation.end_time = datetime.now(timezone.utc)
                self.conversation_manager.end_conversation(self.call_sid)
    
    async def send_twiml_response(self, twiml: str):
        """Send TwiML response to Twilio."""
        # In a real implementation, we would send this over the WebSocket
        response = {
            'event': 'twiml',
            'twiml': twiml,
        }
        # Would be sent via websocket.send(json.dumps(response))


class TwilioEngine:
    """Twilio integration engine for managing calls and media streams."""

    def __init__(self):
        self.streams: Dict[str, MediaStream] = {}
        self.initialized = False

    async def initialize(self):
        self.initialized = True
        logger.info("TwilioEngine initialized")

    def has_audio(self, call_sid: str) -> bool:
        stream = self.streams.get(call_sid)
        return stream is not None and bool(stream.audio_buffer)

    def get_audio(self, call_sid: str) -> Optional[bytes]:
        stream = self.streams.get(call_sid)
        if stream and stream.audio_buffer:
            return stream.audio_buffer.pop(0)
        return None

    async def send_audio(self, call_sid: str, audio_data: bytes):
        stream = self.streams.get(call_sid)
        if stream:
            await stream.send_audio_response(audio_data)

    def register_stream(self, call_sid: str, stream: MediaStream):
        self.streams[call_sid] = stream

    async def cleanup(self):
        for stream in self.streams.values():
            await stream.cleanup()
        self.streams.clear()
        self.initialized = False
        logger.info("TwilioEngine cleaned up")


# Module-level instance
twilio_engine = TwilioEngine()
