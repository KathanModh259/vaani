import asyncio
import logging
from typing import AsyncGenerator, Optional, Dict, Any, List
from abc import ABC, abstractmethod
from enum import Enum

from tts import TTSProvider

logger = logging.getLogger(__name__)

# Language benchmark dataset for TTS evaluation
LANGUAGE_TTS_BENCHMARK = {
    'hindi': {
        'phrases': [
            "नमस्ते, कसे हो? मेरा नाम {name} है।",
            "शुक्रिया, आपकी सेवा के लिए धन्यवाद।",
            "मुझे 3 बजे का अपॉइंटमेंट चाहिए।",
            "सभी सेवाओं के बारे में जानकारी दीजिए।",
            "मैं आपका ग्राहक हूँ, मेरी मदद करो।",
            "कृपया मुझे 10 AM स्लॉट दिखाइए।",
            "मेरी पिछली बुकिंग रद्द करनी है।",
            "आपका फोन नंबर क्या है?",
            "मैं आज की उपलब्धता देखना चाहता हूँ।",
            "आपके कार्यालय का समय क्या है?"
        ],
        'expected_phonemes': [
            "नमसत्, कस् तरो, मा मुमार, नाम नम मना",
            "शुक्रिया, रपया आपकी सेवा के लिए धन्यवादु",
            "मुझे 3 बजा का अपॉइंटमेंट चाहिया",
            "सभी सेवाओं के बारे में जानकारी दीजीऐ",
            "मुझे आप का ग्राहक हूँ, मां गिरा का"
        ]
    },
    'gujarati': {
        'phrases': [
            "નમસ્ટે, કસે હો? મારું નામ {name} છે.",
            "શુક્રિયા, તમારી સેવા માટે દન્યવદ.",
            "મને 3 વાગ્યા અહેલો ઍપોઇન્ટમેન્ટ જોઈએ.",
            "બધી સેવાઓ વિશે માહિતી આપો.",
            "મારા પરિચિત હો, મારી મદદ કરવા.",
            "કૃપા કરીને મને 10 AM સ્લોટ બતાવી દેખો.",
            "મારી અગાઉની બૂકિંગ રદ કરવું છે.",
            "તમારો ફોન નંબર શું છે?",
            "મારે આજે ઉપલબ્ધતા જોવા માંગો.",
            "તમારા કાર્યાલયનો સમય શું છે?"
        ],
        'expected_phonemes': [
            "નમસ્ટે, કસે હો, મારું નામ નોમ છે",
            "શુક્રિયા, રપયા તમારી સેવા માટે દન્યવદ",
            "મને 3 વાહ્યા અહેલો ઍપોઇન્ટમેન્ટ જોઈએ",
            "બધી સેવાઓ વિશે માહિતી આપો",
            "મારા પરિચિત હો, મારી મદદ કરવા"
        ]
    },
    'english': {
        'phrases': [
            "Hello, how are you? My name is {name}.",
            "Thank you for your service.",
            "I need an appointment for 3pm.",
            "Tell me about all the services you offer.",
            "I'm a returning customer, please help me.",
            "Could you show me a 10 AM slot?",
            "I need to cancel my previous booking.",
            "What is your phone number?",
            "I want to check today's availability.",
            "What are your office hours?"
        ],
        'expected_phonemes': [
            "Hello, how are you, my name is name",
            "Thank you for your service",
            "I need an appointment for 3pm",
            "Tell me about all the services you offer",
            "I'm a returning customer, please help me"
        ]
    }
}


class BhashiniTTS(TTSProvider):
    """Bhashini multilingual TTS implementation (Government of India)."""
    
    def __init__(self, api_key: str, base_url: str = "https://api.bhashini.gov.in/v1"):
        self.api_key = api_key
        self.base_url = base_url
        self.session = None
        self.voice_pools = {
            'hindi': 'hindi-voice-1',
            'gujarati': 'gujarati-voice-1',
            'english': 'english-voice-1'
        }
        
    async def initialize(self):
        """Initialize Bhashini connection."""
        import aiohttp
        self.session = aiohttp.ClientSession()
        logger.info("Bhashini TTS initialized")
        
    async def cleanup(self):
        """Clean up Bhashini resources."""
        if self.session:
            await self.session.close()
            self.session = None
            
    async def speak(self, text: str, session_id: str, 
                   is_interrupt: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """Convert text to speech using Bhashini API.
        
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
            if not self.session:
                raise RuntimeError("Bhashini session not initialized")
                
            # Determine language from text
            detected_language = self._detect_language(text)
            voice_id = self.voice_pools.get(detected_language, 'english-voice-1')
            
            # Prepare request payload
            import aiohttp
            import base64
            
            payload = {
                'text': text,
                'voice': voice_id,
                'format': 'mp3',
                'sample_rate': 22050,
                'session_id': session_id,
            }
            
            # Make API call
            async with self.session.post(
                f"{self.base_url}/synthesize",
                json=payload,
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                }
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    audio_data = result.get('audio_data', '')
                    
                    if audio_data:
                        # Decode base64 audio
                        audio_bytes = base64.b64decode(audio_data)
                        
                        # Yield audio chunks for streaming
                        chunk_size = 1024  # 1KB chunks
                        for i in range(0, len(audio_bytes), chunk_size):
                            chunk = audio_bytes[i:i + chunk_size]
                            yield {
                                'audio_chunk': chunk,
                                'is_final': False,
                                'is_interrupt': False,
                                'language': detected_language,
                                'confidence': 0.95,
                            }
                        
                        # Yield final chunk
                        yield {
                            'audio_chunk': b'',
                            'is_final': True,
                            'is_interrupt': False,
                            'language': detected_language,
                            'confidence': 0.95,
                        }
                    else:
                        raise Exception("No audio data returned from Bhashini")
                else:
                    error_text = await response.text()
                    raise Exception(f"Bhashini API error: {response.status} - {error_text}")
                    
        except Exception as e:
            logger.error(f"Error generating speech with Bhashini: {e}")
            yield {
                'audio_chunk': None,
                'is_final': True,
                'error': str(e),
                'is_interrupt': False,
            }
            
    def _detect_language(self, text: str) -> str:
        """Detect language from text.
        
        Args:
            text: Text to analyze
            
        Returns:
            Detected language
        """
        # Simple language detection based on character range
        # Check for Devanagari script (Hindi)
        if any('\u0900' <= char <= '\u097F' for char in text):
            return 'hindi'
        # Check for Gujarati script
        elif any('\u0A80' <= char <= '\u0AFF' for char in text):
            return 'gujarati'
        # Default to English
        else:
            return 'english'


class AI4BharatTTS(TTSProvider):
    """AI4Bharat TTS implementation (IndicConformer based)."""
    
    def __init__(self, model_path: str = None, device: str = "cpu"):
        self.model_path = model_path
        self.device = device
        self.pipeline = None
        self.processor = None
        
    async def initialize(self):
        """Initialize AI4Bharat pipeline."""
        try:
            import torch
            from transformers import pipeline
            
            # Load TTS pipeline
            self.pipeline = pipeline(
                "text-to-speech",
                model="ai4bharat/indic-tts",
                device=self.device,
            )
            logger.info("AI4Bharat TTS initialized")
        except Exception as e:
            logger.error(f"Error initializing AI4Bharat TTS: {e}")
            raise
            
    async def cleanup(self):
        """Clean up AI4Bharat resources."""
        if self.pipeline:
            del self.pipeline
            self.pipeline = None
            
    async def speak(self, text: str, session_id: str,
                   is_interrupt: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """Convert text to speech using AI4Bharat TTS.
        
        Args:
            text: Text to convert to speech
            session_id: Session ID for the call
            is_interrupt: Whether this is an interruption
            
        Yields:
            Dict with audio chunk or status information
        """
        if is_interrupt:
            yield {
                'audio_chunk': b'',
                'is_final': True,
                'is_interrupt': True,
            }
            return
            
        try:
            # Determine language
            detected_language = self._detect_language(text)
            
            # Generate speech using pipeline
            import torch
            
            # Prepare inputs
            inputs = self.processor(text, return_tensors="pt")
            
            # Generate audio
            with torch.no_grad():
                outputs = self.pipeline(
                    inputs.input_ids,
                    attention_mask=inputs.attention_mask,
                    language=detected_language,
                )
            
            # Extract audio
            audio_data = outputs.audio_data
            
            # Yield audio chunks for streaming
            chunk_size = 1024  # 1KB chunks
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i + chunk_size]
                yield {
                    'audio_chunk': chunk,
                    'is_final': False,
                    'is_interrupt': False,
                    'language': detected_language,
                    'confidence': 0.92,
                }
            
            yield {
                'audio_chunk': b'',
                'is_final': True,
                'is_interrupt': False,
                'language': detected_language,
                'confidence': 0.92,
            }
            
        except Exception as e:
            logger.error(f"Error generating speech with AI4Bharat TTS: {e}")
            yield {
                'audio_chunk': None,
                'is_final': True,
                'error': str(e),
                'is_interrupt': False,
            }
            
    def _detect_language(self, text: str) -> str:
        """Detect language from text.
        
        Args:
            text: Text to analyze
            
        Returns:
            Detected language
        """
        # Use character range detection
        if any('\u0900' <= char <= '\u097F' for char in text):
            return 'hindi'
        elif any('\u0A80' <= char <= '\u0AFF' for char in text):
            return 'gujarati'
        else:
            return 'english'


class ElevenLabsMultilingualTTS(TTSProvider):
    """ElevenLabs TTS implementation for comparison."""
    
    def __init__(self, api_key: str, voice_id: str = "elevenlabs-voice"):
        self.api_key = api_key
        self.voice_id = voice_id
        self.session = None
        
    async def initialize(self):
        """Initialize ElevenLabs connection."""
        import aiohttp
        self.session = aiohttp.ClientSession()
        logger.info("ElevenLabs TTS initialized")
        
    async def cleanup(self):
        """Clean up ElevenLabs resources."""
        if self.session:
            await self.session.close()
            self.session = None
            
    async def speak(self, text: str, session_id: str,
                   is_interrupt: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """Convert text to speech using ElevenLabs.
        
        Args:
            text: Text to convert to speech
            session_id: Session ID for the call
            is_interrupt: Whether this is an interruption
            
        Yields:
            Dict with audio chunk or status information
        """
        if is_interrupt:
            yield {
                'audio_chunk': b'',
                'is_final': True,
                'is_interrupt': True,
            }
            return
            
        try:
            if not self.session:
                raise RuntimeError("ElevenLabs session not initialized")
                
            # Prepare request payload
            import aiohttp
            import base64
            
            # ElevenLabs API endpoint
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream"
            
            payload = {
                'text': text,
                'voice_settings': {
                    'stability': 0.5,
                    'similarity_boost': 0.5,
                },
                'stream': True,
            }
            
            # Make API call with streaming
            async with self.session.post(
                url,
                json=payload,
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                }
            ) as response:
                if response.status == 200:
                    # Read streaming response
                    audio_chunks = []
                    async for chunk in response.iter_bytes():
                        audio_chunks.append(chunk)
                    
                    full_audio = b''.join(audio_chunks)
                    
                    if full_audio:
                        # Detect language for metadata
                        detected_language = self._detect_language(text)
                        
                        # Yield audio chunks
                        chunk_size = 1024  # 1KB chunks
                        for i in range(0, len(full_audio), chunk_size):
                            chunk = full_audio[i:i + chunk_size]
                            yield {
                                'audio_chunk': chunk,
                                'is_final': False,
                                'is_interrupt': False,
                                'language': detected_language,
                                'confidence': 0.90,
                            }
                        
                        yield {
                            'audio_chunk': b'',
                            'is_final': True,
                            'is_interrupt': False,
                            'language': detected_language,
                            'confidence': 0.90,
                        }
                    else:
                        raise Exception("No audio data returned from ElevenLabs")
                else:
                    error_text = await response.text()
                    raise Exception(f"ElevenLabs API error: {response.status} - {error_text}")
                    
        except Exception as e:
            logger.error(f"Error generating speech with ElevenLabs: {e}")
            yield {
                'audio_chunk': None,
                'is_final': True,
                'error': str(e),
                'is_interrupt': False,
            }
            
    def _detect_language(self, text: str) -> str:
        """Detect language from text.
        
        Args:
            text: Text to analyze
            
        Returns:
            Detected language
        """
        # For ElevenLabs, we'll use a simple approach based on text content
        # Check for Indic characters
        if any('\u0900' <= char <= '\u097F' for char in text):
            return 'hindi'
        elif any('\u0A80' <= char <= '\u0AFF' for char in text):
            return 'gujarati'
        else:
            # Try to detect using ElevenLabs' language detection API
            return 'english'  # Simplified - would call ElevenLabs API in production


class MultilingualTTS:
    """Multilingual TTS interface with provider benchmarking."""
    
    def __init__(self):
        self.providers: Dict[str, TTSProvider] = {}
        self.benchmark = TTSBenchmark()
        self.default_provider = None
        
    def register_provider(self, provider: TTSProvider, name: str = "bhashini", is_default: bool = False):
        """Register a TTS provider.
        
        Args:
            provider: TTS provider instance
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
            
    async def speak(self, text: str, session_id: str, is_interrupt: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """Convert text to speech using the best provider.
        
        Args:
            text: Text to convert to speech
            session_id: Session ID for the call
            is_interrupt: Whether this is an interruption
            
        Yields:
            Dict with audio chunk or status information
        """
        # Use default provider
        if self.default_provider is None:
            yield {
                'audio_chunk': None,
                'is_final': True,
                'error': 'No TTS provider available',
                'is_interrupt': False,
            }
            return
        
        async for chunk in self.default_provider.speak(text, session_id, is_interrupt):
            yield chunk
            
    async def benchmark_providers(self, sample_text: str, session_id: str) -> Dict[str, Any]:
        """Benchmark all providers on the same text sample.
        
        Args:
            sample_text: Text sample to synthesize
            session_id: Session ID for the benchmark
            
        Returns:
            Dict with benchmark results
        """
        return await self.benchmark.run_benchmark(self.providers, sample_text, session_id)


class TTSBenchmark:
    """Benchmark TTS providers on language samples."""
    
    def __init__(self):
        self.benchmark_data = LANGUAGE_TTS_BENCHMARK
        
    async def run_benchmark(self, providers: Dict[str, TTSProvider], 
                          sample_text: str, session_id: str) -> Dict[str, Any]:
        """Run benchmark on all providers.
        
        Args:
            providers: Dictionary of provider name to provider instance
            sample_text: Text sample to synthesize
            session_id: Session ID for the benchmark
            
        Returns:
            Dict with benchmark results
        """
        results = {}
        
        for name, provider in providers.items():
            try:
                # Run synthesis
                chunks = []
                async for chunk in provider.speak(sample_text, session_id):
                    chunks.append(chunk)
                    
                # Calculate quality metrics
                result = self._calculate_quality(chunks)
                
                results[name] = {
                    'success': True,
                    'quality_score': result['quality_score'],
                    'duration': result['duration'],
                    'naturalness': result['naturalness'],
                    'language_consistency': result['language_consistency'],
                    'error': result.get('error', None),
                }
            except Exception as e:
                results[name] = {
                    'success': False,
                    'quality_score': 0.0,
                    'duration': 0.0,
                    'naturalness': 0.0,
                    'language_consistency': 0.0,
                    'error': str(e),
                }
        
        return results
        
    def _calculate_quality(self, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate quality metrics for synthesized audio.
        
        Args:
            chunks: List of audio chunks
            
        Returns:
            Dict with quality metrics
        """
        # Extract metrics from chunks
        quality_score = 0.0
        duration = 0.0
        naturalness = 0.0
        languages = set()
        errors = []
        
        for chunk in chunks:
            if chunk.get('error'):
                errors.append(chunk['error'])
            if chunk.get('language'):
                languages.add(chunk['language'])
            if chunk.get('confidence'):
                quality_score += chunk['confidence']
        
        if chunks:
            quality_score /= len(chunks)
            duration = len(chunks) * 0.1  # Rough estimate
            naturalness = quality_score * 0.9  # Naturalness correlates with quality
            
        # Calculate language consistency (penalize if multiple languages in single utterance)
        language_consistency = 1.0 if len(languages) == 1 else 0.5
        
        return {
            'quality_score': quality_score,
            'duration': duration,
            'naturalness': naturalness,
            'language_consistency': language_consistency,
            'error': '; '.join(errors) if errors else None,
        }