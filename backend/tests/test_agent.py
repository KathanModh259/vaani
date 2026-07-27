import pytest
from datetime import datetime
from services.conversation_manager import ConversationState, ConversationManager
from llm_agent.agent import LLMAgent, Intent, CheckAvailabilityAction, BookAppointmentAction, AnswerFAQAction


class MockDBManager:
    async def get_session(self):
        return None

    async def close(self):
        pass


@pytest.fixture
def db_manager():
    return MockDBManager()


@pytest.fixture
def agent(db_manager):
    return LLMAgent(db_manager)


@pytest.fixture
def conversation():
    return ConversationState(business_id="test_biz", call_sid="test_call")


class TestConversationState:
    def test_init(self):
        c = ConversationState("biz1", "call1")
        assert c.business_id == "biz1"
        assert c.call_sid == "call1"
        assert c.turns == []
        assert c.escalation_requested is False
        assert c.booking_attempted is False

    def test_add_turn(self):
        c = ConversationState("biz1", "call1")
        c.add_turn("caller", "Hello", 0.95, True)
        assert len(c.turns) == 1
        assert c.turns[0]["speaker"] == "caller"
        assert c.turns[0]["text"] == "Hello"

    def test_should_escalate(self):
        c = ConversationState("biz1", "call1")
        assert c.should_escalate() is False
        c.escalation_requested = True
        assert c.should_escalate() is True

    def test_escalate_after_clarifications(self):
        c = ConversationState("biz1", "call1")
        c.clarification_attempts = 3
        assert c.should_escalate() is True


class TestConversationManager:
    def test_start_and_get(self):
        mgr = ConversationManager()
        c = mgr.start_conversation("biz1", "call1")
        assert mgr.get_conversation("call1") is c

    def test_end_conversation(self):
        mgr = ConversationManager()
        mgr.start_conversation("biz1", "call1")
        mgr.end_conversation("call1")
        assert mgr.get_conversation("call1") is None


class TestLLMAgent:
    def test_init(self, agent):
        assert len(agent.actions) == 6
        assert Intent.BOOK_APPOINTMENT in agent.actions
        assert Intent.ANSWER_FAQ in agent.actions

    def test_extract_intent_book(self, agent, conversation):
        intent = agent._extract_intent("I want to book an appointment", conversation)
        assert intent == Intent.BOOK_APPOINTMENT

    def test_extract_intent_faq(self, agent, conversation):
        intent = agent._extract_intent("What are your hours?", conversation)
        assert intent == Intent.ANSWER_FAQ

    def test_extract_intent_escalate(self, agent, conversation):
        intent = agent._extract_intent("I want to talk to a human agent", conversation)
        assert intent == Intent.ESCALATE_TO_HUMAN

    def test_extract_intent_unknown(self, agent, conversation):
        intent = agent._extract_intent("xyzzy flurbo garble", conversation)
        assert intent == Intent.UNKNOWN

    @pytest.mark.asyncio
    async def test_process_turn_adds_turns(self, agent, conversation):
        result = await agent.process_turn(conversation, None, "What are your hours?")
        assert len(conversation.turns) >= 2
        assert result["conversation"] is conversation

    @pytest.mark.asyncio
    async def test_process_turn_escalation(self, agent, conversation):
        conversation.clarification_attempts = 3
        result = await agent.process_turn(conversation, None, "help")
        assert result["escalated"] is True


class TestActions:
    def test_check_availability(self):
        action = CheckAvailabilityAction()
        schema = action.get_action_schema()
        assert schema["name"] == "check_availability"

    def test_book_appointment(self):
        action = BookAppointmentAction()
        schema = action.get_action_schema()
        assert schema["name"] == "book_appointment"
        assert "customer_name" in schema["parameters"]["properties"]

    def test_answer_faq(self):
        action = AnswerFAQAction()
        schema = action.get_action_schema()
        assert schema["name"] == "get_faq_answer"
