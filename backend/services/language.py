"""Which language a piece of text is in, from its script.

The LLM mirrors the caller's language, but the phone voice and speech
recognition used to stay fixed to the business's setting - so a Hindi reply
was read out by an English voice, and the caller's next Hindi sentence was
transcribed by an English recogniser. Following the reply's script fixes
both, turn by turn.

Script, not vocabulary: Hinglish written in Latin letters stays on the
business default (en-IN recognisers handle romanised Hindi reasonably).
"""

from typing import Optional

_RANGES = {
    "hi-IN": (0x0900, 0x097F),  # Devanagari
    "gu-IN": (0x0A80, 0x0AFF),  # Gujarati
}


def script_language(text: str, min_share: float = 0.3) -> Optional[str]:
    """hi-IN / gu-IN when that script makes up a meaningful share of the
    letters; None for Latin-script text (keep the current language)."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return None
    for lang, (lo, hi) in _RANGES.items():
        share = sum(1 for c in letters if lo <= ord(c) <= hi) / len(letters)
        if share >= min_share:
            return lang
    return None


def reply_language(text: str, default: str) -> str:
    return script_language(text) or default
