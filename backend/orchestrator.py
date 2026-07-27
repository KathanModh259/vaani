"""
Main package for AI Phone Receptionist System.

This package provides a production-grade voice agent that answers real phone calls
for small businesses, handling appointment booking, FAQs, and call routing.
"""

import asyncio
import logging

from services.conversation_manager import ConversationManager
from llm_agent.multilingual_agent import MultilingualLLMAgent
from telephony.twilio_engine import TwilioEngine
from stt.deepgram_stt import DeepgramSTT
from tts.elevenlabs_tts import ElevenLabsTTS
from db import DatabaseManager

logger = logging.getLogger(__name__)

# Main voice agent class that orchestrates all components
class VoiceAgent:
    """
    Main AI Phone Receptionist that orchestrates all components.
    
    This class integrates telephony, STT, TTS, and LLM agent components
    to provide a complete voice interaction system.
    """
    
    def __init__(self):
        self.conversation_manager = ConversationManager()
        self.llm_agent = None
        self.twilio_engine = TwilioEngine()
        self.stt = None
        self.tts = None
        self.db_manager = None
        
    async def initialize(self):
        """Initialize all components."""
        # Initialize database
        from scripts.config import settings
        self.db_manager = DatabaseManager(settings.DATABASE_URL)
        await self.db_manager.initialize()

        # Initialize LLM agent with database (using multilingual version)
        self.llm_agent = MultilingualLLMAgent(self.db_manager)
        await self.llm_agent.initialize()

        # Initialize STT and TTS engines
        self.stt = DeepgramSTT()
        self.tts = ElevenLabsTTS()

        # Initialize Twilio engine
        await self.twilio_engine.initialize()

        print("AI Phone Receptionist initialized successfully!")
    
    async def cleanup(self):
        """Clean up all components."""
        if self.db_manager:
            await self.db_manager.close()
        if self.llm_agent:
            await self.llm_agent.cleanup()
        if self.stt:
            await self.stt.cleanup()
        if self.tts:
            await self.tts.cleanup()
        if self.twilio_engine:
            await self.twilio_engine.cleanup()
        
        print("AI Phone Receptionist cleaned up successfully!")
    
    async def start_call(self, business_id: str, phone_number: str):
        """
        Start a new phone call.

        Args:
            business_id: ID of the business receiving the call
            phone_number: Phone number of the caller

        Returns:
            Call session ID
        """
        # Initialize database manager with URL from settings
        from scripts.config import settings
        self.db_manager = DatabaseManager(settings.DATABASE_URL)
        await self.db_manager.initialize()

        # Create database session
        async with self.db_manager.get_session() as db:
            # Create conversation state
            conversation = self.conversation_manager.start_conversation(business_id, phone_number)
            conversation.add_turn(
                'agent',
                "Welcome to our business! How can I help you today?"
            )

            # Start the call handler
            await self._handle_call(call_sid=phone_number, business_id=business_id, conversation=conversation, db_session=db)

            # Save call to database
            await self._save_call_to_db(conversation, db)

        return phone_number
    
    async def _handle_call(self, call_sid: str, business_id: str, conversation, db_session):
        """Handle the phone call with real-time audio processing."""
        try:
            print(f"Handling call from {call_sid} for business {business_id}")

            # Process conversation turns until end or escalation
            while not conversation.should_escalate():
                # Check if there's new audio input
                if self.twilio_engine.has_audio(call_sid):
                    # Get transcribed text from STT
                    audio_data = self.twilio_engine.get_audio(call_sid)
                    transcript = await self.stt.transcribe(audio_data, call_sid)

                    if transcript and transcript.get('text'):
                        user_text = transcript['text']
                        confidence = transcript.get('confidence', 0.0)
                        language = transcript.get('language', 'unknown')
                        is_code_switched = transcript.get('is_code_switched', False)

                        # Add user turn to conversation
                        conversation.add_turn(
                            'caller',
                            user_text,
                            confidence,
                            transcript.get('is_final', True)
                        )

                        # Add language segment to conversation profile
                        conversation.add_language_segment(language, confidence, is_code_switched)

                        # Generate agent response using multilingual agent
                        response = await self.llm_agent.process_turn(
                            conversation,
                            db_session,  # Pass the database session
                            user_text,
                            conversation.language_profile
                        )

                        # Extract response text and language
                        agent_response = response.get('response', '')
                        response_language = response.get('language', 'unknown')

                        # Convert response to speech
                        audio_response = await self.tts.speak(
                            agent_response,
                            call_sid,
                            is_interrupt=False
                        )

                        # Send audio response back
                        self.twilio_engine.send_audio(call_sid, audio_response)

                        # Check if action was taken
                        if response.get('action'):
                            action_result = response['action']
                            if action_result.get('success'):
                                conversation.booking_attempted = True

                        # Check if escalated
                        if response.get('escalated'):
                            conversation.escalation_requested = True

                # Small delay to prevent busy waiting
                await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Error handling call {call_sid}: {e}")
            conversation.add_turn('agent', "I'm sorry, an error occurred. Please try again later.")
    
    async def _save_call_to_db(self, conversation, db):
        """Save call data to database."""
        from db import CallLogSQL
        
        call_log = CallLogSQL(
            id=f"call_{conversation.call_sid}",
            call_sid=conversation.call_sid,
            business_id=conversation.business_id,
            start_time=conversation.start_time,
            end_time=getattr(conversation, 'end_time', None),
            transcript={"turns": conversation.turns},
            outcome=self._determine_call_outcome(conversation),
            duration_seconds=0,
            customer_phone="unknown",
            agent_confirmed=conversation.booking_attempted,
            escalation_requested=conversation.should_escalate(),
        )
        
        db.add(call_log)
        await db.commit()
    
    def _determine_call_outcome(self, conversation) -> str:
        """Determine the outcome of the call."""
        if conversation.should_escalate():
            return "escalated"
        elif conversation.booking_attempted:
            return "booked"
        else:
            return "completed_faq"