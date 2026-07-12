"""Password hashing (Argon2id) and session-token hashing.

Pure, dependency-light helpers so they can be unit-tested without a DB or LLM.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()  # Argon2id defaults (see ADR-012)


def hash_password(password: str) -> str:
    """Return an Argon2id PHC-string hash. Used by scripts/hash_password.py."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time-ish verification. Never raises on a bad password."""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        # Malformed hash string, etc. — treat as auth failure, don't crash the request.
        return False


def generate_session_token() -> str:
    """A high-entropy opaque token; the plaintext goes only into the httpOnly cookie."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str, secret: str) -> str:
    """Deterministic HMAC-SHA256 of a session token; only the hash is stored in the DB."""
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
