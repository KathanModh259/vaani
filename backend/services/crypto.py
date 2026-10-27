"""Application-level encryption for sensitive call data.

Call transcripts are the most sensitive thing this product stores - they can
contain medical details, addresses, card-adjacent information and anything else
a caller happens to say. They were being written to the database as plaintext
JSON while the marketing site promised they were "encrypted at rest".

This closes that gap: transcripts are encrypted with Fernet (AES-128-CBC +
HMAC) before they touch the database, using ENCRYPTION_KEY from the
environment.

Reads are deliberately tolerant: rows written before this existed are still
plaintext, so decrypt() falls back to returning them as-is rather than failing.
That makes the change safe to deploy against an existing database.

Losing ENCRYPTION_KEY means losing every stored transcript - back it up
separately from the database itself, or the backup and the key share a fate.
"""

import base64
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_fernet = None
_checked = False

# Marks a value as produced by this module, so decrypt() can tell an encrypted
# blob apart from a legacy plaintext row without guessing.
PREFIX = "enc:v1:"


def backend_available() -> bool:
    """Is the encryption library actually importable?

    It was installed transitively in development and missing from the built
    image for months, which silently downgraded every transcript to
    plaintext. The production startup check calls this so a missing library
    stops the deploy instead of quietly weakening it.
    """
    try:
        from cryptography.fernet import Fernet  # noqa: F401
        return True
    except ImportError:
        return False


def _get_fernet():
    """Build the cipher once. Returns None when no key is configured."""
    global _fernet, _checked

    if _checked:
        return _fernet
    _checked = True

    from scripts.config import settings

    raw = (settings.ENCRYPTION_KEY or "").strip()
    if not raw:
        logger.warning(
            "ENCRYPTION_KEY is not set - call transcripts will be stored in "
            "plaintext. Set it before handling real customer calls."
        )
        return None

    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        logger.error("cryptography not installed, cannot encrypt transcripts: %s", e)
        return None

    # Accept any passphrase: Fernet needs exactly 32 url-safe base64 bytes, and
    # requiring operators to generate that exact shape invites them to skip it.
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    _fernet = Fernet(key)
    return _fernet


def is_enabled() -> bool:
    return _get_fernet() is not None


def encrypt_json(value: Any) -> Any:
    """Encrypt a JSON-serialisable value. Returns plaintext if no key is set."""
    f = _get_fernet()
    if f is None or value is None:
        return value
    try:
        token = f.encrypt(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return PREFIX + token.decode("ascii")
    except Exception as e:
        # Never lose a transcript because encryption failed.
        logger.error("Transcript encryption failed, storing plaintext: %s", e)
        return value


def decrypt_json(value: Any, default: Any = None) -> Any:
    """Decrypt a value written by encrypt_json.

    Plaintext values (written before encryption was enabled, or while no key
    was configured) are returned unchanged.
    """
    if value is None:
        return default

    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value  # legacy plaintext row

    f = _get_fernet()
    if f is None:
        logger.error("Encrypted transcript found but ENCRYPTION_KEY is not set")
        return default

    try:
        plain = f.decrypt(value[len(PREFIX):].encode("ascii"))
        return json.loads(plain.decode("utf-8"))
    except Exception as e:
        # Wrong key, or tampering. Do not crash the dashboard over one row.
        logger.error("Transcript decryption failed: %s", e)
        return default
