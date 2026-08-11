"""Real LLM responses for the receptionist, with a rules-engine fallback.

The agent in agent.py is a keyword matcher returning fixed strings. That is a
sane offline default, but it cannot answer anything it was not scripted for.
When OPENAI_API_KEY is configured this module takes over and produces genuine
replies; without a key, or if the call fails, the caller falls back to the
rules engine so the demo never dies mid-conversation.
"""

import logging
from typing import Any, Dict, List, Optional

from scripts.config import settings

logger = logging.getLogger(__name__)

_client = None
_unavailable_reason = None

# Kept short on purpose: this text is prepended to every turn, and a phone
# receptionist needs terse, speakable answers rather than prose.
SYSTEM_PROMPT = """You are Vaani, the phone receptionist for {business_name}.

You are speaking on a live phone call. Follow these rules:
- Keep replies under 40 words. One or two sentences. This is speech, not text.
- Never use markdown, bullet points, emoji or formatting of any kind.
- Speak naturally, warmly and directly, as a skilled human receptionist would.
- Reply in the language the caller used. If they mix Hindi and English, mirror them.
- To book an appointment you need: service, date, time, caller name, phone number.
  Ask for whatever is still missing, one or two items at a time, never all at once.
- Never invent prices, availability, medical or legal advice. If you do not know,
  say you will have a colleague confirm and offer to take a message.
- If the caller is angry, or asks for a human, say you are transferring them.

{business_context}"""

DEFAULT_CONTEXT = """Business details:
- Hours: 9:00 AM to 5:00 PM, Monday to Friday.
- Payment: credit card or cash at the time of service.
- Cancellations: 24 hours notice required for a refund."""


def is_configured() -> bool:
    return bool(settings.OPENAI_API_KEY)


def status() -> Dict[str, Any]:
    if not is_configured():
        return {
            "available": False,
            "detail": "OPENAI_API_KEY not set - using built-in rules engine",
            "model": None,
        }
    return {
        "available": _unavailable_reason is None,
        "detail": _unavailable_reason,
        "model": settings.LLM_MODEL,
    }


def _get_client():
    """Lazily build the AsyncOpenAI client. Returns None when unavailable."""
    global _client, _unavailable_reason

    if _client is not None:
        return _client
    if not is_configured():
        return None

    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        _unavailable_reason = f"openai package not installed: {e}"
        logger.warning("LLM unavailable: %s", _unavailable_reason)
        return None

    try:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, timeout=12.0)
        _unavailable_reason = None
    except Exception as e:
        _unavailable_reason = str(e)
        logger.warning("LLM client init failed: %s", e)
        return None

    return _client


def _build_messages(turns: List[Dict[str, Any]], user_text: str,
                    business_name: str, business_context: str) -> List[Dict[str, str]]:
    messages = [{
        "role": "system",
        "content": SYSTEM_PROMPT.format(
            business_name=business_name or "this business",
            business_context=business_context or DEFAULT_CONTEXT,
        ),
    }]

    # Only the tail of the call matters, and it keeps latency and cost down.
    for turn in turns[-10:]:
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        role = "user" if turn.get("speaker") == "caller" else "assistant"
        messages.append({"role": role, "content": text})

    # add_turn() is called before this, so the latest user line may already be
    # in the history - do not send it twice.
    if not (messages[-1]["role"] == "user" and messages[-1]["content"] == user_text.strip()):
        messages.append({"role": "user", "content": user_text})

    return messages


async def generate(
    turns: List[Dict[str, Any]],
    user_text: str,
    business_name: str = "",
    business_context: str = "",
) -> Optional[str]:
    """Generate a reply, or None to signal "fall back to the rules engine"."""
    global _unavailable_reason

    client = _get_client()
    if client is None:
        return None

    try:
        resp = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=_build_messages(turns, user_text, business_name, business_context),
            max_tokens=120,
            temperature=0.6,
        )
    except Exception as e:
        # Rate limits, bad key, network - all non-fatal; the caller degrades.
        _unavailable_reason = f"{type(e).__name__}: {e}"
        logger.warning("LLM call failed, falling back to rules engine: %s", e)
        return None

    _unavailable_reason = None
    text = (resp.choices[0].message.content or "").strip()
    return text or None
