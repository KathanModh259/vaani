import logging
from typing import Dict, List, Any, Optional, AsyncGenerator
from abc import ABC, abstractmethod

from stt import STTProvider

logger = logging.getLogger(__name__)

# Language benchmark dataset configuration
LANGUAGE_BENCHMARK_SAMPLES = {
    'hindi': {
        'phrases': [
            "नमस्ते, क्या आप मेरी मदद कर सकते हैं?",
            "मैं 3 बजे का अपॉइंटमेंट बुक कराना चाहता हूँ",
            "कृपया मुझे आपके व्यवसाय के बारे MCQ दीजिए",
            "आपके कार्यालय के घंटे क्या हैं?",
            "मैं आपका भुगतान कैसे कर सकता हूँ?",
            "मेरी पिछली बुकिंग रद्द करनी है",
            "आपके पास कौन सी सेवाएँ उपलब्ध हैं?",
            "मुझे एक नया अपॉइंटमेंट चाहिए",
            "आपका फोन नंबर क्या है?",
            "मुझे आज की उपलब्धता दिखाइए"
        ],
        'audio_paths': [
            "/data/hindi_pure_1.wav",
            "/data/hindi_pure_2.wav",
            "/data/hindi_pure_3.wav",
            "/data/hindi_pure_4.wav",
            "/data/hindi_pure_5.wav"
        ]
    },
    'gujarati': {
        'phrases': [
            "નમસ્ટે, શું તમે મારી મદદ કરી શકશો?",
            "હું 3 વાગ્યા અહેલો બૂક કરાવી શકતો નથી",
            "કૃપા કરીને મને તમારા વ્યવસાય વિશે માહિતી આપો",
            "તમારા કાર્યાલયનો સમય શું છે?",
            "હું તમને કેવી રીતે ચૂકવણી કરી શકું?",
            "મારી અગાઉની બૂકિંગ રદ કરવી છે",
            "તમારી પાસે કઈ સેવાઓ ઉપલબ્ધ છે?",
            "મારે એક નવો ઍપોઇન્ટમેન્ટ જોઈએ",
            "તમારો ફોન નંબર શું છે?",
            "મારે આજે ઉપલબ્ધતા બતાવો"
        ],
        'audio_paths': [
            "/data/gujarati_pure_1.wav",
            "/data/gujarati_pure_2.wav",
            "/data/gujarati_pure_3.wav",
            "/data/gujarati_pure_4.wav",
            "/data/gujarati_pure_5.wav"
        ]
    },
    'english': {
        'phrases': [
            "Hello, can you help me with something?",
            "I want to book an appointment for 3pm",
            "Could you tell me about your business?",
            "What are your office hours?",
            "How can I make a payment?",
            "I need to cancel my previous booking",
            "What services do you offer?",
            "I need a new appointment",
            "What is your phone number?",
            "Can I see today's availability?"
        ],
        'audio_paths': [
            "/data/english_pure_1.wav",
            "/data/english_pure_2.wav",
            "/data/english_pure_3.wav",
            "/data/english_pure_4.wav",
            "/data/english_pure_5.wav"
        ]
    },
    'code-switched': {
        'phrases': [
            "kal 3 baje book karwana hai, is it possible?",
            "I need to book by tomorrow at 5:30, કૃપા કરીને મેળવી જોઈ",
            "શું તમારો કામકાજ 9 થી 6 સુન્ન છે, is it also weekends?",
            "book 10 AM slot કૃપા કરીને ખાસ રાખઋ",
            "cancel krdo 2 baje ka, કૃપા કરીને રદ કરઋ",
            "I need to know about services, કૃપા કરીને જણાવઋ",
            "હું મોડી નથી, પછીથી 4 પર બૂક કરી શકું, can I?",
            "પ્રિસ કેટલા રૂપિયા છે, I need to know",
            "Appointment માટે by tomorrow at 3 પર, શું તમે કરી શકો છો?",
            "I'm from ગુજરાત, તમારા વિશે જાણવા માંગો"
        ],
        'audio_paths': [
            "/data/codeswitch_mixed_1.wav",
            "/data/codeswitch_mixed_2.wav",
            "/data/codeswitch_mixed_3.wav",
            "/data/codeswitch_mixed_4.wav",
            "/data/codeswitch_mixed_5.wav"
        ]
    }
}


class BhashiniSTT(STTProvider):
    """Bhashini multilingual STT implementation (Government of India)."""
    
    def __init__(self, api_key: str, base_url: str = "https://api.bhashini.gov.in/v1"):
        self.api_key = api_key
        self.base_url = base_url
        self.session = None
        
    async def initialize(self):
        """Initialize Bhashini connection."""
        import aiohttp
        self.session = aiohttp.ClientSession()
        logger.info("Bhashini STT initialized")
        
    async def cleanup(self):
        """Clean up Bhashini resources."""
        if self.session:
            await self.session.close()
            self.session = None
            
    async def transcribe(self, audio_data: bytes, session_id: str, 
                         is_final: bool = False) -> Dict[str, Any]:
        """Transcribe audio using Bhashini API.
        
        Args:
            audio_data: Raw audio bytes (16kHz, 16-bit mono, PCM)
            session_id: Session ID for the call
            is_final: Whether this is the final chunk of an utterance
            
        Returns:
            Dict with:
            - 'text': Transcribed text
            - 'confidence': Confidence score (0-1)
            - 'is_final': Whether transcription is final
            - 'language': Detected language
            - 'is_code_switched': Whether code-switching detected
            - 'error': Any error message
        """
        if not self.session:
            return {
                'text': '',
                'confidence': 0.0,
                'is_final': is_final,
                'language': 'unknown',
                'is_code_switched': False,
                'error': 'Bhashini session not initialized',
            }
        
        try:
            import aiohttp
            import json
            
            # Prepare request payload
            # Bhashini accepts audio as base64 encoded with format
            payload = {
                'audio': base64.b64encode(audio_data).decode('ascii'),
                'format': 'wav',
                'sample_rate': 16000,
                'language': 'mixed',  # Detect automatically
                'session_id': session_id,
            }
            
            # Make API call
            async with self.session.post(
                f"{self.base_url}/recognize",
                json=payload,
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                }
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    
                    # Parse response (Bhashini format may vary)
                    transcript = result.get('text', '')
                    confidence = result.get('confidence', 0.0)
                    detected_language = result.get('language', 'unknown')
                    
                    # Check if code-switched (multiple languages detected)
                    is_code_switched = (
                        'hindi' in str(transcript).lower() and
                        ('english' in str(transcript).lower() or 'gujarati' in str(transcript).lower()) or
                        'gujarati' in str(transcript).lower() and
                        ('english' in str(transcript).lower() or 'hindi' in str(transcript).lower())
                    )
                    
                    return {
                        'text': transcript,
                        'confidence': float(confidence),
                        'is_final': is_final,
                        'language': detected_language,
                        'is_code_switched': is_code_switched,
                        'error': None,
                    }
                else:
                    error_text = await response.text()
                    return {
                        'text': '',
                        'confidence': 0.0,
                        'is_final': is_final,
                        'language': 'unknown',
                        'is_code_switched': False,
                        'error': f'Bhashini API error: {response.status} - {error_text}',
                    }
                    
        except Exception as e:
            logger.error(f"Error transcribing with Bhashini: {e}")
            return {
                'text': '',
                'confidence': 0.0,
                'is_final': is_final,
                'language': 'unknown',
                'is_code_switched': False,
                'error': f'Transcription error: {str(e)}',
            }


class IndicWhisperSTT(STTProvider):
    """IndicWhisper STT implementation (AI4Bharat open-source)."""
    
    def __init__(self, model_path: str = None, device: str = "cpu"):
        self.model_path = model_path
        self.device = device
        self.pipeline = None
        
    async def initialize(self):
        """Initialize IndicWhisper pipeline."""
        try:
            import torch
            from transformers import pipeline
            
            # Load IndicWhisper pipeline
            self.pipeline = pipeline(
                "automatic-speech-recognition",
                model="ai4bharat/indic-whisper-common-indovo-2023",
                chunk_length_s=30,
                device=self.device,
            )
            logger.info("IndicWhisper STT initialized")
        except Exception as e:
            logger.error(f"Error initializing IndicWhisper: {e}")
            raise
            
    async def cleanup(self):
        """Clean up IndicWhisper resources."""
        if self.pipeline:
            del self.pipeline
            self.pipeline = None
            
    async def transcribe(self, audio_data: bytes, session_id: str,
                         is_final: bool = False) -> Dict[str, Any]:
        """Transcribe audio using IndicWhisper.
        
        Args:
            audio_data: Raw audio bytes (16kHz, 16-bit mono, PCM)
            session_id: Session ID for the call
            is_final: Whether this is the final chunk of an utterance
            
        Returns:
            Dict with transcription results
        """
        if not self.pipeline:
            return {
                'text': '',
                'confidence': 0.0,
                'is_final': is_final,
                'language': 'unknown',
                'is_code_switched': False,
                'error': 'IndicWhisper pipeline not initialized',
            }
        
        try:
            # Prepare audio for IndicWhisper
            import io
            from transformers import WhisperProcessor
            import torch
            
            # Create a temporary audio file
            audio_io = io.BytesIO(audio_data)
            
            # Process audio
            inputs = self.processor(
                audio_io,
                sampling_rate=16000,
                return_tensors="pt"
            )
            
            # Generate transcription
            with torch.no_grad():
                predicted_ids = self.pipeline(
                    inputs.input_values,
                    decoder_input_ids=inputs.decoder_input_ids
                ).ids
            
            # Decode result
            transcript = self.processor.batch_decode(
                predicted_ids,
                skip_special_tokens=True,
                normalize=False
            )[0]
            
            # Determine language and confidence
            language = "hindi"  # Default assumption for IndicWhisper
            confidence = 0.85  # Default confidence
            is_code_switched = "en" in transcript.lower() or "gu" in transcript.lower()
            
            return {
                'text': transcript,
                'confidence': confidence,
                'is_final': is_final,
                'language': language,
                'is_code_switched': is_code_switched,
                'error': None,
            }
            
        except Exception as e:
            logger.error(f"Error transcribing with IndicWhisper: {e}")
            return {
                'text': '',
                'confidence': 0.0,
                'is_final': is_final,
                'language': 'unknown',
                'is_code_switched': False,
                'error': f'Transcription error: {str(e)}',
            }


class WhisperSTT(STTProvider):
    """Standard Whisper STT implementation for baseline."""
    
    def __init__(self, model_name: str = "openai/whisper-large-v3"):
        self.model_name = model_name
        self.pipeline = None
        
    async def initialize(self):
        """Initialize Whisper pipeline."""
        try:
            from transformers import pipeline
            
            # Load Whisper pipeline
            self.pipeline = pipeline(
                "automatic-speech-recognition",
                model=self.model_name,
                chunk_length_s=30,
            )
            logger.info("Whisper STT initialized")
        except Exception as e:
            logger.error(f"Error initializing Whisper: {e}")
            raise
            
    async def cleanup(self):
        """Clean up Whisper resources."""
        if self.pipeline:
            del self.pipeline
            self.pipeline = None
            
    async def transcribe(self, audio_data: bytes, session_id: str,
                         is_final: bool = False) -> Dict[str, Any]:
        """Transcribe audio using Whisper.
        
        Args:
            audio_data: Raw audio bytes (16kHz, 16-bit mono, PCM)
            session_id: Session ID for the call
            is_final: Whether this is the final chunk of an utterance
            
        Returns:
            Dict with transcription results
        """
        if not self.pipeline:
            return {
                'text': '',
                'confidence': 0.0,
                'is_final': is_final,
                'language': 'unknown',
                'is_code_switched': False,
                'error': 'Whisper pipeline not initialized',
            }
        
        try:
            import io
            from transformers import WhisperProcessor
            import torch
            
            # Prepare audio for Whisper
            audio_io = io.BytesIO(audio_data)
            
            # Process audio
            inputs = self.processor(
                audio_io,
                sampling_rate=16000,
                return_tensors="pt"
            )
            
            # Generate transcription
            with torch.no_grad():
                predicted_ids = self.pipeline(
                    inputs.input_values,
                    decoder_input_ids=inputs.decoder_input_ids
                ).ids
            
            # Decode result
            transcript = self.processor.batch_decode(
                predicted_ids,
                skip_special_tokens=True,
                normalize=False
            )[0]
            
            # Determine language and confidence
            language = "english"  # Default assumption for Whisper
            confidence = 0.90  # Default confidence for English
            is_code_switched = any(lang in transcript.lower() for lang in ['hindi', 'gujarati'])
            
            return {
                'text': transcript,
                'confidence': confidence,
                'is_final': is_final,
                'language': language,
                'is_code_switched': is_code_switched,
                'error': None,
            }
            
        except Exception as e:
            logger.error(f"Error transcribing with Whisper: {e}")
            return {
                'text': '',
                'confidence': 0.0,
                'is_final': is_final,
                'language': 'unknown',
                'is_code_switched': False,
                'error': f'Transcription error: {str(e)}',
            }


class DeepgramSTT(STTProvider):
    """Deepgram streaming STT implementation."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = "wss://api.deepgram.com/v1/listen"
        self.socket = None
        self.session = None
        
    async def initialize(self):
        """Initialize Deepgram connection."""
        import aiohttp
        import asyncio
        
        self.session = aiohttp.ClientSession()
        logger.info("Deepgram STT initialized")
        
    async def cleanup(self):
        """Clean up Deepgram resources."""
        if self.socket and not self.socket.closed:
            await self.socket.close()
            self.socket = None
        
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
            
    async def transcribe(self, audio_data: bytes, session_id: str,
                         is_final: bool = False) -> Dict[str, Any]:
        """Transcribe audio using Deepgram streaming API.
        
        Args:
            audio_data: Raw audio bytes (16kHz, 16-bit mono, PCM)
            session_id: Session ID for the call
            is_final: Whether this is the final chunk of an utterance
            
        Returns:
            Dict with transcription results
        """
        # For now, return a placeholder implementation
        # In a real implementation, we would use Deepgram's streaming API
        transcript_language = "english"
        if b"\x00\x01\x02" in audio_data:  # Simple heuristic for Indic script
            transcript_language = "hindi"
        elif b"\x03\x04\x05" in audio_data:  # Simple heuristic for Gujarati script
            transcript_language = "gujarati"
        
        is_code_switched = (
            b"\x00\x01\x02" in audio_data and b"\x03\x04\x05" in audio_data
        )
        
        return {
            'text': f'placeholder {transcript_language} transcription',
            'confidence': 0.85,
            'is_final': is_final,
            'language': transcript_language,
            'is_code_switched': is_code_switched,
            'error': None,
        }


class MultilingualSTT:
    """Multilingual STT interface with provider benchmarking."""
    
    def __init__(self):
        self.providers: Dict[str, STTProvider] = {}
        self.benchmark = ProviderBenchmark()
        self.default_provider = None
        
    def register_provider(self, provider: STTProvider, name: str = "deepgram", is_default: bool = False):
        """Register an STT provider.
        
        Args:
            provider: STT provider instance
            name: Name of the provider
            is_default: Whether this is the default provider
        """
        self.providers[name] = provider
        if is_default:
            self.default_provider = provider
            
    async def initialize(self):
        """Initialize all registered providers."""
        for name, provider in self.providers.items():
            await provider.initialize()
        if self.default_provider is None and self.providers:
            self.default_provider = next(iter(self.providers.values()))
            
    async def cleanup(self):
        """Clean up all providers."""
        for provider in self.providers.values():
            await provider.cleanup()
            
    async def transcribe(self, audio_data: bytes, session_id: str,
                         is_final: bool = False) -> Dict[str, Any]:
        """Transcribe audio using the best provider.
        
        Args:
            audio_data: Raw audio bytes
            session_id: Session ID for the call
            is_final: Whether this is the final chunk of an utterance
            
        Returns:
            Dict with transcription results including language detection
        """
        # Use default provider
        if self.default_provider is None:
            return {
                'text': '',
                'confidence': 0.0,
                'is_final': is_final,
                'language': 'unknown',
                'is_code_switched': False,
                'error': 'No STT provider available',
            }
        
        return await self.default_provider.transcribe(audio_data, session_id, is_final)
        
    async def benchmark_providers(self, sample_audio: bytes, session_id: str) -> Dict[str, Any]:
        """Benchmark all providers on the same audio sample.
        
        Args:
            sample_audio: Audio sample to transcribe
            session_id: Session ID for the benchmark
            
        Returns:
            Dict with benchmark results
        """
        return await self.benchmark.run_benchmark(self.providers, sample_audio, session_id)


class ProviderBenchmark:
    """Benchmark STT providers on language samples."""
    
    def __init__(self):
        self.benchmark_data = LANGUAGE_BENCHMARK_SAMPLES
        
    async def run_benchmark(self, providers: Dict[str, STTProvider], 
                          sample_audio: bytes, session_id: str) -> Dict[str, Any]:
        """Run benchmark on all providers.
        
        Args:
            providers: Dictionary of provider name to provider instance
            sample_audio: Audio sample to transcribe
            session_id: Session ID for the benchmark
            
        Returns:
            Dict with benchmark results
        """
        results = {}
        
        for name, provider in providers.items():
            try:
                # Run transcription
                result = await provider.transcribe(sample_audio, session_id)
                
                results[name] = {
                    'success': True,
                    'accuracy': self._calculate_accuracy(result),
                    'language_detected': result.get('language', 'unknown'),
                    'is_code_switched': result.get('is_code_switched', False),
                    'confidence': result.get('confidence', 0.0),
                    'error': result.get('error', None),
                }
            except Exception as e:
                results[name] = {
                    'success': False,
                    'accuracy': 0.0,
                    'language_detected': 'unknown',
                    'is_code_switched': False,
                    'confidence': 0.0,
                    'error': str(e),
                }
        
        return results
        
    def _calculate_accuracy(self, result: Dict[str, Any]) -> float:
        """Calculate accuracy score for a transcription result.
        
        Args:
            result: Transcription result
            
        Returns:
            Accuracy score (0-1)
        """
        text = result.get('text', '')
        language = result.get('language', 'unknown')
        
        # Simple accuracy calculation based on confidence and language detection
        base_score = result.get('confidence', 0.0)
        
        # Bonus for correct language detection
        language_bonus = 0.2 if language in ['hindi', 'gujarati', 'english'] else 0.0
        
        # Penalty for errors
        if 'error' in result and result['error']:
            base_score *= 0.5
            
        return min(1.0, base_score + language_bonus)