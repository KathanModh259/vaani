from typing import Optional, Dict, Any, List, TYPE_CHECKING
from datetime import datetime, timedelta, timezone
import asyncio
import logging

if TYPE_CHECKING:
    from llm_agent.multilingual_agent import LanguageProfile

logger = logging.getLogger(__name__)


class STTProvider:
    """Abstract base class for Speech-to-Text providers."""
    
    async def initialize(self):
        """Initialize the STT provider."""
        pass
    
    async def cleanup(self):
        """Clean up the STT provider."""
        pass
    
    async def transcribe(self, audio_data: bytes, session_id: str) -> Dict[str, Any]:
        """Transcribe audio data.
        
        Returns:
            Dict with keys:
            - 'text': transcribed text
            - 'confidence': confidence score (0-1)
            - 'is_final': whether transcription is final
            - 'error': any error message
        """
        raise NotImplementedError()


class ConversationState:
    """Manages conversation state across a call."""

    def __init__(self, business_id: str, call_sid: str):
        self.business_id = business_id
        self.call_sid = call_sid
        self.turns: List[Dict[str, Any]] = []
        self.context: Dict[str, Any] = {}
        self.intent: Optional[str] = None
        self.confidence: float = 0.0
        self.escalation_requested: bool = False
        self.booking_attempted: bool = False
        self.clarification_attempts: int = 0
        self.silence_timeout: Optional[datetime] = None
        self.start_time: datetime = datetime.now(timezone.utc)
        self.end_time: Optional[datetime] = None
        # Language tracking
        from llm_agent.multilingual_agent import LanguageProfile
        self.language_profile = LanguageProfile()
    
    def add_turn(self, speaker: str, text: str, confidence: float = 1.0, is_final: bool = True):
        """Add a turn to the conversation."""
        turn = {
            'timestamp': datetime.now(timezone.utc),
            'speaker': speaker,
            'text': text,
            'confidence': confidence,
            'is_final': is_final,
        }
        self.turns.append(turn)
    
    def get_context(self) -> str:
        """Get conversation context for LLM."""
        context_parts = []
        
        # Add last 10 turns for context
        recent_turns = self.turns[-10:] if len(self.turns) >= 10 else self.turns
        
        for turn in recent_turns:
            speaker = "USER" if turn['speaker'] == 'caller' else "AGENT"
            context_parts.append(f"{speaker}: {turn['text']}")
        
        return "\n".join(context_parts)
    
    def update_intent(self, intent: str, confidence: float):
        """Update the detected intent."""
        self.intent = intent
        self.confidence = confidence
        self.add_turn('agent', f"Detected intent: {intent} with confidence {confidence}")
    
    def increment_clarification(self):
        """Increment clarification attempt counter."""
        self.clarification_attempts += 1
    
    def should_escalate(self) -> bool:
        """Check if conversation should be escalated."""
        return self.escalation_requested or self.clarification_attempts >= 2
    
    def reset(self):
        """Reset conversation state for new call."""
        self.turns = []
        self.context = {}
        self.intent = None
        self.confidence = 0.0
        self.escalation_requested = False
        self.booking_attempted = False
        self.clarification_attempts = 0
        self.silence_timeout = None
        self.start_time = datetime.now(timezone.utc)
        # Reset language tracking
        from llm_agent.multilingual_agent import LanguageProfile
        self.language_profile = LanguageProfile()

    def add_language_segment(self, language: str, confidence: float, is_code_switched: bool = False):
        """Add a detected language segment to the conversation."""
        self.language_profile.add_segment(language, confidence, is_code_switched)

    def get_current_language(self) -> str:
        """Get the most recently detected language."""
        if self.language_profile.segmented_languages:
            return self.language_profile.segmented_languages[-1].language
        return "unknown"

    def get_language_mix(self) -> List[str]:
        """Get list of languages used in conversation."""
        return self.language_profile.language_mix

    def get_code_switch_count(self) -> int:
        """Get number of code-switching events."""
        return self.language_profile.code_switch_count

    def get_predominant_language(self) -> str:
        """Get the predominant language in conversation."""
        return self.language_profile.get_predominant()


class ConversationManager:
    """Manages conversation state across calls."""
    
    def __init__(self):
        self.active_conversations: Dict[str, ConversationState] = {}
    
    def start_conversation(self, business_id: str, call_sid: str) -> ConversationState:
        """Start a new conversation."""
        conversation = ConversationState(business_id, call_sid)
        self.active_conversations[call_sid] = conversation
        return conversation
    
    def get_conversation(self, call_sid: str) -> Optional[ConversationState]:
        """Get conversation state."""
        return self.active_conversations.get(call_sid)
    
    def end_conversation(self, call_sid: str):
        """End a conversation."""
        if call_sid in self.active_conversations:
            del self.active_conversations[call_sid]
    
    def cleanup_inactive(self, timeout_minutes: int = 30):
        """Clean up inactive conversations."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        expired_calls = []
        
        for call_sid, conversation in self.active_conversations.items():
            if conversation.end_time and conversation.end_time < cutoff_time:
                expired_calls.append(call_sid)
        
        for call_sid in expired_calls:
            self.end_conversation(call_sid)
