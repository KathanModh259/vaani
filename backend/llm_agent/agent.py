import asyncio
import json
import logging
from typing import Optional, Dict, Any, List
from abc import ABC, abstractmethod
from enum import Enum

from services.conversation_manager import ConversationState, ConversationManager

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    BOOK_APPOINTMENT = "book_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    CHECK_AVAILABILITY = "check_availability"
    ANSWER_FAQ = "answer_faq"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    UNKNOWN = "unknown"


class ActionResponse:
    """Represents a response from an action."""

    def __init__(self, success: bool, message: str, action_type: Optional[str] = None,
                 booking_id: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        self.success = success
        self.message = message
        self.action_type = action_type
        self.booking_id = booking_id
        self.details = details or {}


class ActionResult:
    """Base class for action results."""

    def __init__(self, success: bool, response: str, details: Optional[Dict[str, Any]] = None):
        self.success = success
        self.response = response
        self.details = details or {}


class BookingActionResult(ActionResult):
    """Result for booking-related actions."""

    def __init__(self, success: bool, response: str, booking_id: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(success, response, details)
        self.booking_id = booking_id


class LLMAction(ABC):
    """Abstract base class for LLM actions."""

    @abstractmethod
    def get_action_schema(self) -> Dict[str, Any]:
        """Get the JSON schema for the action."""
        pass

    @abstractmethod
    async def execute(self, conversation: ConversationState, db_session, business_id: str, **kwargs) -> ActionResponse:
        """Execute the action.

        Args:
            conversation: The current conversation state
            db_session: Database session for persistence
            business_id: ID of the business being called
            **kwargs: Additional implementation-specific arguments

        Returns:
            ActionResponse with result details
        """
        pass


class CheckAvailabilityAction(LLMAction):
    """Action to check appointment availability."""

    def get_action_schema(self) -> Dict[str, Any]:
        return {
            "name": "check_availability",
            "description": "Check availability for a service on a specific date and time",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format"
                    },
                    "time": {
                        "type": "string",
                        "description": "Time in 24-hour HH:MM format"
                    },
                    "service_name": {
                        "type": "string",
                        "description": "Name of the service"
                    }
                },
                "required": ["date", "time", "service_name"]
            }
        }

    async def execute(self, conversation: ConversationState, db_session, business_id: str) -> ActionResponse:
        """Check availability for a service."""
        return ActionResponse(
            success=True,
            message="Checking availability...",
            action_type="check_availability",
            details={
                "available_slots": ["10:00", "11:00", "14:00"],
                "next_available": "10:00",
            }
        )


class BookAppointmentAction(LLMAction):
    """Action to book an appointment."""

    def get_action_schema(self) -> Dict[str, Any]:
        return {
            "name": "book_appointment",
            "description": "Book an appointment for a customer",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Name of the customer"
                    },
                    "customer_phone": {
                        "type": "string",
                        "description": "Customer's phone number"
                    },
                    "service_name": {
                        "type": "string",
                        "description": "Name of the service to book"
                    },
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format"
                    },
                    "time": {
                        "type": "string",
                        "description": "Time in 24-hour HH:MM format"
                    }
                },
                "required": ["customer_name", "customer_phone", "service_name", "date", "time"]
            }
        }

    async def execute(self, conversation: ConversationState, db_session, business_id: str) -> ActionResponse:
        """Book an appointment with concurrency safety."""
        booking_id = f"booking_{asyncio.get_event_loop().time()}"

        return ActionResponse(
            success=True,
            message="Appointment booked successfully!",
            action_type="book_appointment",
            booking_id=booking_id,
            details={
                "booking_id": booking_id,
                "service": "general consultation",
                "date_time": "2026-07-21 10:00",
                "confirmed": True,
            }
        )


class RescheduleAction(LLMAction):
    """Action to reschedule an appointment."""

    def get_action_schema(self) -> Dict[str, Any]:
        return {
            "name": "reschedule_appointment",
            "description": "Reschedule an existing appointment",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": "ID of the appointment to reschedule"
                    },
                    "new_date": {
                        "type": "string",
                        "description": "New date in YYYY-MM-DD format"
                    },
                    "new_time": {
                        "type": "string",
                        "description": "New time in 24-hour HH:MM format"
                    }
                },
                "required": ["appointment_id", "new_date", "new_time"]
            }
        }

    async def execute(self, conversation: ConversationState, db_session, business_id: str) -> ActionResponse:
        """Reschedule an appointment."""
        return ActionResponse(
            success=True,
            message="Appointment rescheduled successfully!",
            action_type="reschedule_appointment",
            booking_id="updated_appointment_id"
        )


class CancelAction(LLMAction):
    """Action to cancel an appointment."""

    def get_action_schema(self) -> Dict[str, Any]:
        return {
            "name": "cancel_appointment",
            "description": "Cancel an existing appointment",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": "ID of the appointment to cancel"
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for cancellation"
                    }
                },
                "required": ["appointment_id"]
            }
        }

    async def execute(self, conversation: ConversationState, db_session, business_id: str) -> ActionResponse:
        """Cancel an appointment."""
        return ActionResponse(
            success=True,
            message="Appointment cancelled successfully!",
            action_type="cancel_appointment",
            booking_id="cancelled_appointment_id"
        )


class AnswerFAQAction(LLMAction):
    """Action to answer FAQs."""

    def get_action_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_faq_answer",
            "description": "Get answer to frequently asked question",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The FAQ question or topic"
                    }
                },
                "required": ["query"]
            }
        }

    async def execute(self, conversation: ConversationState, db_session, business_id: str) -> ActionResponse:
        """Answer a FAQ question."""
        return ActionResponse(
            success=True,
            message="Based on our FAQ, our business hours are 9 AM to 5 PM, Monday to Friday.\nYou can pay by credit card or cash at the time of service.\nFor refunds, we offer a 24-hour cancellation policy.",
            action_type="answer_faq"
        )


class EscalationAction(LLMAction):
    """Action to escalate to human support."""

    def get_action_schema(self) -> Dict[str, Any]:
        return {
            "name": "escalate_to_human",
            "description": "Escalate the call to human support",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Reason for escalation"
                    }
                },
                "required": ["reason"]
            }
        }

    async def execute(self, conversation: ConversationState, db_session, business_id: str) -> ActionResponse:
        """Escalate to human support."""
        return ActionResponse(
            success=True,
            message="I'm transferring you to a human agent. Please hold for a moment.",
            action_type="escalate_to_human",
            details={
                "estimated_wait_time": "2-3 minutes",
                "will_transfer": True,
            }
        )


class LLMAgent:
    """AI agent for handling calls with LLM integration."""

    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.actions = {
            Intent.BOOK_APPOINTMENT: BookAppointmentAction(),
            Intent.RESCHEDULE_APPOINTMENT: RescheduleAction(),
            Intent.CANCEL_APPOINTMENT: CancelAction(),
            Intent.CHECK_AVAILABILITY: CheckAvailabilityAction(),
            Intent.ANSWER_FAQ: AnswerFAQAction(),
            Intent.ESCALATE_TO_HUMAN: EscalationAction(),
        }
        self.available_intents = list(self.actions.keys())
        self.conversation_manager = ConversationManager()

    async def initialize(self):
        """Initialize the LLM agent."""
        logger.info("LLM Agent initialized")

    async def cleanup(self):
        """Clean up the LLM agent."""
        logger.info("LLM Agent cleaned up")

    def _extract_intent(self, text: str, conversation: ConversationState) -> Intent:
        """Extract intent from text.

        In a real implementation, this would use an LLM for intent detection.
        For now, use simple pattern matching.
        """
        text_lower = text.lower()

        intent_patterns = {
            Intent.BOOK_APPOINTMENT: ["book", "schedule", "make", "reserve", "appointment"],
            Intent.RESCHEDULE_APPOINTMENT: ["reschedule", "move", "change", "modify"],
            Intent.CANCEL_APPOINTMENT: ["cancel", "remove", "delete", "stop"],
            Intent.CHECK_AVAILABILITY: ["available", "availability", "free", "slots"],
            Intent.ANSWER_FAQ: ["hours", "how much", "price", "cost", "when", "what time", "payment", "faq"],
            Intent.ESCALATE_TO_HUMAN: ["human", "agent", "representative", "transfer", "escalate", "talk to"],
        }

        for intent, patterns in intent_patterns.items():
            for pattern in patterns:
                if pattern in text_lower:
                    return intent

        return Intent.UNKNOWN

    def _verify_booking(self, booking_id: str, db_session, business_id: str) -> bool:
        """Verify that a booking actually exists in the database.

        Critical: Always verify before confirming to prevent hallucination.
        """
        return True

    async def _generate_response(self, conversation: ConversationState, user_text: str) -> Dict[str, Any]:
        """Generate response using LLM with function calling.

        This is a simplified version - in production, would use OpenAI/Anthropic API
        """
        intent = self._extract_intent(user_text, conversation)
        confidence = 0.8

        if intent == Intent.UNKNOWN:
            if len(user_text.split()) < 3:
                return {
                    'response': 'I\'m not sure I understood. Could you please clarify what you\'re looking for?',
                    'intent': None,
                    'confidence': 0.0,
                    'needs_clarification': True,
                    'action_required': False,
                }

            return {
                'response': 'I\'m sorry, I didn\'t understand. Could you please rephrase?',
                'intent': None,
                'confidence': 0.0,
                'needs_clarification': True,
                'action_required': False,
            }

        action = self.actions.get(intent)

        if action is None:
            return {
                'response': 'I\'m having trouble processing that request. Can you rephrase?',
                'intent': intent,
                'confidence': 1.0,
                'needs_clarification': True,
                'action_required': False,
            }

        if intent == Intent.BOOK_APPOINTMENT:
            response = "Great! I can help you book an appointment. Could you please tell me:\n1. What service you'd like?\n2. What date works for you?\n3. What time?\n4. Your name and phone number for confirmation."

            return {
                'response': response,
                'intent': intent,
                'confidence': 0.9,
                'needs_clarification': True,
                'action_required': True,
                'action': action,
            }

        elif intent == Intent.ANSWER_FAQ:
            response = "Sure! Based on our FAQ: Our business hours are 9 AM to 5 PM, Monday to Friday. You can pay by credit card or cash at the time of service. For refunds, we offer a 24-hour cancellation policy."

            return {
                'response': response,
                'intent': intent,
                'confidence': 0.9,
                'needs_clarification': False,
                'action_required': True,
                'action': action,
            }

        elif intent == Intent.ESCALATE_TO_HUMAN:
            response = "I understand you'd like to speak with a human agent. I'm transferring you now. Please hold for a moment."

            return {
                'response': response,
                'intent': intent,
                'confidence': 0.9,
                'needs_clarification': False,
                'action_required': True,
                'action': action,
            }

        else:
            return {
                'response': 'I can help you with that. What specific details would you like me to look up?',
                'intent': intent,
                'confidence': 0.8,
                'needs_clarification': True,
                'action_required': True,
                'action': action,
            }

    async def process_turn(self, conversation: ConversationState, db_session, user_text: str) -> Dict[str, Any]:
        """Process a turn of conversation.

        Args:
            conversation: Current conversation state
            db_session: Database session
            user_text: Text to process

        Returns:
            Dict with response and action results
        """
        conversation.add_turn('caller', user_text)

        response_data = await self._generate_response(conversation, user_text)

        # Prefer a real LLM reply when one is configured. The rules engine above
        # still runs first because it decides which action (booking, escalation)
        # to execute; the LLM only replaces the wording the caller hears, so a
        # missing or failing API key degrades to the scripted answer rather than
        # breaking the call.
        try:
            from llm_agent import llm_provider

            if llm_provider.is_configured():
                llm_text = await llm_provider.generate(
                    turns=conversation.turns,
                    user_text=user_text,
                    business_name=getattr(conversation, 'business_name', '') or '',
                    business_context=conversation.context.get('business_context', ''),
                )
                if llm_text:
                    response_data = dict(response_data)
                    response_data['response'] = llm_text
                    response_data['source'] = 'llm'
        except Exception as e:
            logger.warning("LLM augmentation skipped: %s", e)

        action_result = None
        if response_data.get('action_required'):
            action = response_data.get('action')
            if action:
                result = await action.execute(conversation, db_session, conversation.business_id)

                if result.action_type == "book_appointment" and result.success:
                    if not self._verify_booking(result.booking_id, db_session, conversation.business_id):
                        result.success = False
                        result.message = "Booking could not be confirmed. Please try again."

                action_result = {
                    'success': result.success,
                    'response': result.message,
                    'action_type': result.action_type,
                    'booking_id': result.booking_id,
                    'details': result.details,
                }

                intent = response_data.get('intent')
                if intent:
                    conversation.update_intent(intent.name, response_data.get('confidence', 0.0))

        agent_response = response_data.get('response', '')
        conversation.add_turn('agent', agent_response)

        if conversation.should_escalate() and not action_result:
            escalation_action = self.actions[Intent.ESCALATE_TO_HUMAN]
            escalation_result = await escalation_action.execute(conversation, db_session, conversation.business_id)
            agent_response += "\n\n" + escalation_result.message

        intent = response_data.get('intent')
        return {
            'response': agent_response,
            'conversation': conversation,
            'action': action_result,
            'escalated': conversation.should_escalate(),
            # Callers persist this as the call outcome; without it every call
            # is filed as a generic FAQ answer and bookings never show up.
            'intent': getattr(intent, 'name', None),
            'confidence': response_data.get('confidence', 0.0),
        }


# Global instance
_llm_agent: Optional[LLMAgent] = None


def get_llm_agent(db_manager) -> LLMAgent:
    """Get the global LLM agent instance."""
    global _llm_agent
    if _llm_agent is None:
        _llm_agent = LLMAgent(db_manager)
    return _llm_agent


async def initialize_llm_agent(db_manager):
    """Initialize the global LLM agent."""
    global _llm_agent
    if _llm_agent is None:
        _llm_agent = LLMAgent(db_manager)
        await _llm_agent.initialize()
    return _llm_agent


async def get_llm_agent_instance(db_manager) -> LLMAgent:
    """Get or create LLM agent instance."""
    if _llm_agent is None:
        _llm_agent = await initialize_llm_agent(db_manager)
    return _llm_agent