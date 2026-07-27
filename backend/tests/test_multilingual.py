import pytest
from llm_agent.multilingual_agent import (
    LanguageProfile,
    DetectedLanguage,
    MultilingualLLMAgent,
    MultilingualBookAppointmentAction,
    MultilingualAnswerFAQAction,
)
from llm_agent.agent import Intent
from services.conversation_manager import ConversationState


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
    return MultilingualLLMAgent(db_manager)


@pytest.fixture
def conversation():
    return ConversationState(business_id="test_biz", call_sid="test_call")


class TestLanguageProfile:
    def test_empty_profile(self):
        lp = LanguageProfile()
        assert lp.get_predominant() == "unknown"

    def test_single_language(self):
        lp = LanguageProfile()
        lp.add_segment("hindi", 0.95)
        assert lp.get_predominant() == "hindi"

    def test_mixed_languages(self):
        lp = LanguageProfile()
        lp.add_segment("hindi", 0.95)
        lp.add_segment("english", 0.90)
        lp.add_segment("hindi", 0.85)
        assert lp.get_predominant() == "hindi"

    def test_code_switch_count(self):
        lp = LanguageProfile()
        lp.add_segment("hindi", 0.95, is_code_switched=True)
        lp.add_segment("english", 0.90, is_code_switched=True)
        assert lp.code_switch_count == 2


class TestDetectedLanguage:
    def test_init(self):
        dl = DetectedLanguage("hindi", 0.95, False)
        assert dl.language == "hindi"
        assert dl.confidence == 0.95
        assert dl.is_code_switched is False


class TestMultilingualLLMAgent:
    def test_init(self, agent):
        assert agent.primary_language == "hindi"
        assert len(agent.actions) == 6

    def test_mirror_response_language(self, agent):
        lp = LanguageProfile()
        lp.add_segment("gujarati", 0.95)
        lang = agent._mirror_response_language("test text", lp)
        assert lang == "gujarati"

    @pytest.mark.asyncio
    async def test_process_turn(self, agent, conversation):
        lp = LanguageProfile()
        lp.add_segment("english", 0.95)
        result = await agent.process_turn(conversation, None, "book an appointment", lp)
        assert "response" in result
        assert "language" in result


class TestMultilingualActions:
    def test_book_appointment_schema(self):
        action = MultilingualBookAppointmentAction()
        schema = action.get_action_schema()
        assert schema["name"] == "book_appointment"
        assert "language_preference" in schema["parameters"]["properties"]

    def test_answer_faq_schema(self):
        action = MultilingualAnswerFAQAction()
        schema = action.get_action_schema()
        assert schema["name"] == "get_faq_answer"
        assert "language" in schema["parameters"]["required"]
