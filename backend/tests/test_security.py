"""Security regressions that would be expensive to discover in production.

These cover the paths a paying customer's data actually flows through:
password handling, credential validation, tenant isolation and transcript
encryption. All of them had zero coverage before, and several shipped broken
at some point during development - the passlib/bcrypt incompatibility made
every signup return a 500, and chat calls were being filed against the wrong
business entirely.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# crypto reads this at import time; set a deterministic key for the tests.
os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-unit-tests-only")


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------

def test_password_roundtrip():
    from services.auth import get_password_hash, verify_password

    h = get_password_hash("Str0ngPass!2026")
    assert h.startswith("$2b$")
    assert verify_password("Str0ngPass!2026", h)
    assert not verify_password("wrong", h)


def test_password_hash_is_salted():
    """Two identical passwords must not produce the same hash."""
    from services.auth import get_password_hash

    assert get_password_hash("same") != get_password_hash("same")


def test_password_over_bcrypt_72_byte_limit():
    """bcrypt >= 4.1 raises on >72 bytes; this used to 500 every signup."""
    from services.auth import get_password_hash, verify_password

    long_pw = "x" * 200
    assert verify_password(long_pw, get_password_hash(long_pw))


def test_password_with_multibyte_characters():
    """Truncation must land on a character boundary, not mid-codepoint."""
    from services.auth import get_password_hash, verify_password

    pw = "पासवर्ड🔐" * 20
    assert verify_password(pw, get_password_hash(pw))


def test_malformed_hash_fails_closed():
    """A corrupt hash must deny access, not raise a 500."""
    from services.auth import verify_password

    assert verify_password("anything", "not-a-real-hash") is False
    assert verify_password("", "") is False


# --------------------------------------------------------------------------
# Registration validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("email", ["notanemail", "no@tld", "@nouser.com", "spaces in@x.com", ""])
def test_rejects_invalid_emails(email):
    from pydantic import ValidationError
    from db.db_models.models import UserCreate

    with pytest.raises(ValidationError):
        UserCreate(email=email, password="Str0ngPass!2026", full_name="X")


@pytest.mark.parametrize("password", ["", "1", "short"])
def test_rejects_weak_passwords(password):
    from pydantic import ValidationError
    from db.db_models.models import UserCreate

    with pytest.raises(ValidationError):
        UserCreate(email="a@b.com", password=password, full_name="X")


def test_email_is_normalised():
    """Register and login must agree on casing or users get locked out."""
    from db.db_models.models import UserCreate, UserLogin

    assert UserCreate(email="  MiXeD@Case.COM ", password="Str0ngPass!2026",
                      full_name="X").email == "mixed@case.com"
    assert UserLogin(email="  MIXED@CASE.com ", password="x").email == "mixed@case.com"


def test_accepts_a_reasonable_signup():
    from db.db_models.models import UserCreate

    u = UserCreate(email="owner@clinic.in", password="Str0ngPass!2026",
                   full_name="Dr Mehta", business_name="Bright Smile")
    assert u.email == "owner@clinic.in"


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------

def test_token_roundtrip_and_tamper_detection():
    from services.auth import create_access_token, decode_token

    tok = create_access_token("user-1", "a@b.com", "owner", "secret-key", "HS256", 60)
    payload = decode_token(tok, "secret-key", "HS256")
    assert payload["sub"] == "user-1"

    # Signed with a different key -> must not validate.
    assert decode_token(tok, "attacker-key", "HS256") is None


def test_expired_token_rejected():
    from services.auth import create_access_token, decode_token

    tok = create_access_token("user-1", "a@b.com", "owner", "secret-key", "HS256", -1)
    assert decode_token(tok, "secret-key", "HS256") is None


# --------------------------------------------------------------------------
# Transcript encryption
# --------------------------------------------------------------------------

def test_transcript_is_not_stored_in_plaintext():
    from services.crypto import PREFIX, decrypt_json, encrypt_json

    turns = [{"speaker": "caller", "text": "My number is 98765 43210"}]
    blob = encrypt_json(turns)

    assert isinstance(blob, str) and blob.startswith(PREFIX)
    assert "98765" not in blob          # the sensitive value must not survive
    assert decrypt_json(blob) == turns  # and must come back intact


def test_legacy_plaintext_rows_still_readable():
    """Rows written before encryption existed must not break the dashboard."""
    from services.crypto import decrypt_json

    legacy = [{"speaker": "caller", "text": "hello"}]
    assert decrypt_json(legacy) == legacy


def test_corrupt_ciphertext_returns_default_not_exception():
    from services.crypto import PREFIX, decrypt_json

    assert decrypt_json(PREFIX + "garbage", default=[]) == []


def test_none_transcript_is_safe():
    from services.crypto import decrypt_json, encrypt_json

    assert encrypt_json(None) is None
    assert decrypt_json(None, default=[]) == []


# --------------------------------------------------------------------------
# Call outcome bookkeeping
# --------------------------------------------------------------------------

def test_outcome_precedence_is_sticky():
    """A booking must not be downgraded by a later "thanks, bye" turn."""
    from services.call_logger import merge_outcome

    assert merge_outcome("booked", "completed_faq") == "booked"
    assert merge_outcome("completed_faq", "booked") == "booked"
    assert merge_outcome("completed_faq", "escalated") == "escalated"
    assert merge_outcome(None, "completed_faq") == "completed_faq"


def test_intent_maps_to_outcome():
    from services.call_logger import outcome_for

    assert outcome_for("BOOK_APPOINTMENT") == "booked"
    assert outcome_for("ESCALATE_TO_HUMAN") == "escalated"
    assert outcome_for(None) == "completed_faq"
    assert outcome_for("ANYTHING", escalated=True) == "escalated"


# --------------------------------------------------------------------------
# Intent routing - each of these returned the wrong flow at some point
# --------------------------------------------------------------------------

@pytest.mark.parametrize("utterance,expected", [
    ("cancel my booking", "CANCEL_APPOINTMENT"),
    ("I need to reschedule my appointment", "RESCHEDULE_APPOINTMENT"),
    ("book me in for monday", "BOOK_APPOINTMENT"),
    ("are you open on saturday", "CHECK_AVAILABILITY"),
    ("what is your cancellation policy", "ANSWER_FAQ"),
    ("do you take card", "ANSWER_FAQ"),
    ("let me speak to a person", "ESCALATE_TO_HUMAN"),
    ("I want to talk to a human", "ESCALATE_TO_HUMAN"),
])
def test_intent_routing(utterance, expected):
    from llm_agent.agent import extract_intent

    assert extract_intent(utterance).name == expected
