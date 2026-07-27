import asyncio
import logging
from typing import AsyncGenerator, Optional, Dict, Any, List
from abc import ABC, abstractmethod
from enum import Enum

from llm_agent.agent import LLMAction, ActionResponse, Intent

logger = logging.getLogger(__name__)

# Multilingual actions
class MultilingualActionResponse(ActionResponse):
    """Action response optimized for multilingual support."""
    
    def __init__(self, success: bool, message: str, action_type: Optional[str] = None,
                 booking_id: Optional[str] = None, details: Optional[Dict[str, Any]] = None,
                 response_language: Optional[str] = None):
        super().__init__(success, message, action_type, booking_id, details)
        self.response_language = response_language


class MultilingualBookAppointmentAction(LLMAction):
    """Action to book an appointment with language support."""
    
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
                    },
                    "language_preference": {
                        "type": "string",
                        "description": "Preferred language for response"
                    }
                },
                "required": ["customer_name", "customer_phone", "service_name", "date", "time"]
            }
        }
    
    async def execute(self, conversation, db_session, business_id: str, language_profile) -> ActionResponse:
        """Execute the action with language awareness."""
        # Extract parameters from conversation
        user_text = ""
        if len(conversation.turns) >= 2:
            user_text = conversation.turns[-2]['text']
        
        # In a real implementation, we would parse parameters and book with concurrency safety
        # For now, return a placeholder with language info
        import asyncio
        from datetime import datetime
        
        booked_time = datetime.now()
        
        return MultilingualActionResponse(
            success=True,
            message=f"સફળતાપૂર્વkick: તમારો {language_profile.get_predominant()} ભાષામાં એપોઇન્ટમેન્ટ બુક કરવામાં આવ્યો.",
            action_type="book_appointment",
            booking_id=f"booking_{asyncio.get_event_loop().time()}",
            details={
                "booking_id": f"booking_{asyncio.get_event_loop().time()}",
                "language": language_profile.get_predominant(),
                "booked_at": booked_time.isoformat(),
            },
            response_language=language_profile.get_predominant()
        )


class MultilingualRescheduleAction(LLMAction):
    """Action to reschedule an appointment with language support."""
    
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
    
    async def execute(self, conversation, db_session, business_id: str, language_profile) -> ActionResponse:
        """Reschedule an appointment."""
        return MultilingualActionResponse(
            success=True,
            message=f"એપોઇન્ટમેન્ટ {language_profile.get_predominant()} ભાષામાં સફળતાપૂર્વક ફરીથી સેડ્યૂલ કરવામાં આવ્યો.",
            action_type="reschedule_appointment",
            booking_id="rescheduled_appointment_id",
            details={
                "language": language_profile.get_predominant(),
            },
            response_language=language_profile.get_predominant()
        )


class MultilingualCancelAction(LLMAction):
    """Action to cancel an appointment with language support."""
    
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
    
    async def execute(self, conversation, db_session, business_id: str, language_profile) -> ActionResponse:
        """Cancel an appointment."""
        return MultilingualActionResponse(
            success=True,
            message=f"એપોઇન્ટમેન્ટ {language_profile.get_predominant()} ભાષામાં સફળતાપૂર્વક રદ કરવામાં આવ્યો.",
            action_type="cancel_appointment",
            booking_id="cancelled_appointment_id",
            details={
                "language": language_profile.get_predominant(),
            },
            response_language=language_profile.get_predominant()
        )


class MultilingualAnswerFAQAction(LLMAction):
    """Action to answer FAQs with language support."""
    
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
                    },
                    "language": {
                        "type": "string",
                        "description": "Language for the response"
                    }
                },
                "required": ["query", "language"]
            }
        }
    
    async def execute(self, conversation, db_session, business_id: str, language_profile) -> ActionResponse:
        """Answer a FAQ question with language support."""
        response_language = language_profile.get_predominant()
        
        faq_database = {
            "hindi": (
                "અમારી વ્યવસાયના વિશે સંપરકક્રિયા કરો:\n"
                "- વ્યવસાયનો સમય: સોમવાર-શનિવાર, 9 AM-5 PM\n"
                "- ચુકવણી: ક્રેડિટ કાર્ડ અથવા રોકડ પૈસા બંને માટે મંજૂરી\n"
                "- રિફન્ડ પાલિસી: 24-કલાની કૅન્સેલેશન નીતિ\n"
                "- સેવાઓ: કન્સલ્ટેશન, અસ્સેસ્મેન્ટ, ફોલો-અપ સર્વિસસ\n"
                "\n"
                "ફોન: +91-XXXXXX-XXXX | ઇમેલ: info@yourbusiness.com"
            ),
            "gujarati": (
                "અમારી વ્યવસાયના વિશે સંપરકક્રિયા કરો:\n"
                "- વ્યવસાયનો સમય: સોમવાર-શનિવાર, 9 AM-5 PM\n"
                "- ચુકવણી: ક્રેડિટ કાર્ડ અથવા રોકડ પૈસા બંને માટે મંજૂરી\n"
                "- રિફન્ડ પાલિસી: 24-કલાની કૅન્સેલેશન નીતિ\n"
                "- સેવાઓ: કન્સલ્ટેશન, અસ્સેસ્મેન્ટ, ફોલો-અપ સર્વિસસ\n"
                "\n"
                "ફોન: +91-XXXXXX-XXXX | ઇમેલ: info@yourbusiness.com"
            ),
            "english": (
                "Contact us about our business:\n"
                "- Business Hours: Monday-Friday, 9 AM-5 PM\n"
                "- Payment: Credit card or cash at both locations\n"
                "- Refund Policy: 24-hour cancellation policy\n"
                "- Services: Consultation, Assessment, Follow-up Support Services\n"
                "\n"
                "Phone: +91-XXXXXX-XXXX | Email: info@yourbusiness.com"
            )
        }
        
        return MultilingualActionResponse(
            success=True,
            message=faq_database.get(response_language, faq_database["english"]),
            action_type="answer_faq",
            details={
                "language": response_language,
                "faq_type": "business_info",
            },
            response_language=response_language
        )


class MultilingualEscalationAction(LLMAction):
    """Action to escalate to human support with language support."""
    
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
    
    async def execute(self, conversation, db_session, business_id: str, language_profile) -> ActionResponse:
        """Escalate to human support."""
        response_language = language_profile.get_predominant()
        
        # Language-specific escalation messages
        escalation_messages = {
            "hindi": "હું સમજુ છું કે તમને વધુ સહાયતાની જરૂર છે. હું તમને હમણાં માનવ એજન્ટ સાથે જોડતો જોઈ રહ્યો છું. કૃપા કરીને પ્રતીક્ષા કરો.",
            "gujarati": "હું સમજુ છું કે તમને વધુ સહાયતાની જરૂર છે. હું તમને હમણાં માનવ એજન્ટ સાથે જોડતો જોઈ રહ્યો છું. કૃપા કરીને ધીરા રહો.",
            "english": "I understand you need more assistance. I'm transferring you to a human agent right now. Please hold for a moment."
        }
        
        return MultilingualActionResponse(
            success=True,
            message=escalation_messages.get(response_language, escalation_messages["english"]),
            action_type="escalate_to_human",
            details={
                "language": response_language,
                "estimated_wait_time": "2-3 minutes",
                "will_transfer": True,
            },
            response_language=response_language
        )


class MultilingualCheckAvailabilityAction(LLMAction):
    """Action to check appointment availability with language support."""
    
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
                    },
                    "language": {
                        "type": "string",
                        "description": "Language for the response"
                    }
                },
                "required": ["date", "time", "service_name", "language"]
            }
        }
    
    async def execute(self, conversation, db_session, business_id: str, language_profile) -> ActionResponse:
        """Check availability for a service."""
        response_language = language_profile.get_predominant()
        
        # Mock available slots
        available_slots = {
            "hindi": ["10:00", "11:00", "14:00", "15:30"],
            "gujarati": ["10:00", "11:00", "14:00", "15:30"],
            "english": ["10:00", "11:00", "14:00", "15:30"]
        }
        
        slots_text = ", ".join(available_slots.get(response_language, available_slots["english"]))
        
        messages = {
            "hindi": f"તમારી {response_language} ભાષામાં: {slots_text} સ્થાનો ઉપલબ્ધ છે. કૃપા કરીને બુકિંગ માટે યોગ્ય સમય પસંદ કરો.",
            "gujarati": f"તમારી {response_language} ભાષામાં: {slots_text} સ્થાનો ઉપલબ્ધ છે. કૃપા કરીને બુકિંગ માટે યોગ્ય સમય પસંદ કરો.",
            "english": f"Available {response_language} slots: {slots_text}. Please select a time for booking."
        }
        
        return MultilingualActionResponse(
            success=True,
            message=messages.get(response_language, messages["english"]),
            action_type="check_availability",
            details={
                "language": response_language,
                "available_slots": available_slots.get(response_language, available_slots["english"]),
                "next_available": available_slots.get(response_language, available_slots["english"])[0],
            },
            response_language=response_language
        )


class DetectedLanguage:
    """Represents detected language for a segment of speech."""

    def __init__(self, language: str, confidence: float, is_code_switched: bool = False):
        self.language = language
        self.confidence = confidence
        self.is_code_switched = is_code_switched


class LanguageProfile:
    """Tracks language usage throughout a conversation."""

    def __init__(self):
        self.segmented_languages: List[DetectedLanguage] = []
        self.language_mix = []
        self.code_switch_count = 0

    def add_segment(self, language: str, confidence: float, is_code_switched: bool = False):
        detected = DetectedLanguage(language, confidence, is_code_switched)
        self.segmented_languages.append(detected)
        if language not in self.language_mix:
            self.language_mix.append(language)
        if is_code_switched:
            self.code_switch_count += 1

    def get_predominant(self) -> str:
        if not self.segmented_languages:
            return "unknown"
        counts = {}
        for s in self.segmented_languages:
            counts[s.language] = counts.get(s.language, 0) + 1
        return max(counts.items(), key=lambda x: x[1])[0]


class MultilingualLLMAgent:
    """LLM agent that understands and responds in the caller's language(s)."""

    def __init__(self, db_manager, primary_language: str = "hindi"):
        self.db_manager = db_manager
        self.primary_language = primary_language
        self.actions = {
            Intent.BOOK_APPOINTMENT: MultilingualBookAppointmentAction(),
            Intent.RESCHEDULE_APPOINTMENT: MultilingualRescheduleAction(),
            Intent.CANCEL_APPOINTMENT: MultilingualCancelAction(),
            Intent.CHECK_AVAILABILITY: MultilingualCheckAvailabilityAction(),
            Intent.ANSWER_FAQ: MultilingualAnswerFAQAction(),
            Intent.ESCALATE_TO_HUMAN: MultilingualEscalationAction(),
        }
        self.conversation_manager = None  # Would be ConversationManager()

    async def initialize(self):
        logger.info(f"Multilingual LLM Agent initialized (primary: {self.primary_language})")

    async def cleanup(self):
        logger.info("Multilingual LLM Agent cleaned up")

    def _detect_intent_multilingual(self, text: str, language_profile: LanguageProfile) -> Intent:
        text_lower = text.lower()
        intent_patterns = {
            Intent.BOOK_APPOINTMENT: {
                "hindi": ["बुक", "बुक कर", "अपॉइंटमेंट"],
                "gujarati": ["બુક", "બુક કર", "એપોઇન્ટમેન્ટ"],
                "english": ["book", "schedule", "appointment"]
            },
            Intent.CHECK_AVAILABILITY: {
                "english": ["check", "availability", "available", "slots"]
            },
            Intent.ANSWER_FAQ: {
                "english": ["how", "what", "info", "faq", "question"]
            },
            Intent.ESCALATE_TO_HUMAN: {
                "english": ["human", "agent", "representative", "transfer"]
            }
        }
        for intent, patterns in intent_patterns.items():
            for lang in language_profile.language_mix or ["english"]:
                for pattern in patterns.get(lang, patterns.get("english", [])):
                    if pattern in text_lower:
                        return intent
        return Intent.UNKNOWN

    def _mirror_response_language(self, text: str, language_profile: LanguageProfile) -> str:
        if language_profile.segmented_languages:
            return language_profile.segmented_languages[-1].language
        if language_profile.language_mix:
            return language_profile.language_mix[0]
        return self.primary_language

    async def _generate_multilingual_response(self, conversation, user_text: str,
                                              language_profile: LanguageProfile) -> Dict[str, Any]:
        response_language = self._mirror_response_language(user_text, language_profile)
        intent = self._detect_intent_multilingual(user_text, language_profile)
        if intent == Intent.UNKNOWN:
            return {
                'response': "I'm not sure I understood. Could you please clarify?",
                'intent': intent, 'confidence': 0.0, 'needs_clarification': True,
                'action_required': False, 'response_language': response_language,
            }
        action = self.actions.get(intent)
        return {
            'response': f"Let me help you with that ({intent.value}) in {response_language}",
            'intent': intent, 'confidence': 0.9, 'needs_clarification': False,
            'action_required': True, 'action': action, 'response_language': response_language,
        }

    async def process_turn(self, conversation, db_session, user_text: str,
                           language_profile: LanguageProfile) -> Dict[str, Any]:
        conversation.add_turn('caller', user_text)
        response_data = await self._generate_multilingual_response(conversation, user_text, language_profile)
        action_result = None
        if response_data.get('action_required'):
            action = response_data.get('action')
            if action:
                result = await action.execute(conversation, db_session, conversation.business_id, language_profile)
                action_result = {
                    'success': result.success, 'response': result.message,
                    'action_type': result.action_type, 'booking_id': result.booking_id,
                }
        agent_response = response_data.get('response', '')
        conversation.add_turn('agent', agent_response)
        return {
            'response': agent_response, 'language': response_data.get('response_language', self.primary_language),
            'conversation': conversation, 'action': action_result, 'escalated': conversation.should_escalate(),
        }
