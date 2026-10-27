"""Phone number normalisation.

Twilio sends E.164 ("+919876543210"); callers read their number out as
"98765 43210" or "098765 43210"; owners type "+91-98765-43210". All of these
must compare equal, or call routing and "find my booking" silently miss.

This is deliberately not a full libphonenumber: it covers the formats an
Indian SMB product actually sees and passes anything else through as digits.
"""

import re
from typing import Optional

DEFAULT_COUNTRY_CODE = "91"


def normalize_phone(raw: Optional[str], country_code: str = DEFAULT_COUNTRY_CODE) -> Optional[str]:
    """Best-effort E.164. Returns None for input with no usable digits."""
    if not raw:
        return None
    s = str(raw).strip()
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None

    if has_plus:
        return "+" + digits
    if digits.startswith("00"):                      # international prefix
        return "+" + digits[2:]
    if len(digits) == 10:                            # national mobile/landline
        return f"+{country_code}{digits}"
    if len(digits) == 11 and digits.startswith("0"):  # trunk prefix 0
        return f"+{country_code}{digits[1:]}"
    if len(digits) == 12 and digits.startswith(country_code):
        return "+" + digits
    # Short codes, extensions, foreign numbers without "+": keep the digits.
    return digits


def same_number(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = normalize_phone(a), normalize_phone(b)
    return bool(na) and na == nb


def spoken_digits(e164: Optional[str]) -> str:
    """"+919876543210" -> "9 8 7 6 5, 4 3 2 1 0" so TTS reads digits, not a number."""
    if not e164:
        return ""
    d = re.sub(r"\D", "", e164)[-10:]
    return " ".join(d[:5]) + ", " + " ".join(d[5:])
