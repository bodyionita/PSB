"""Authentication service: password login, session issue/validate/revoke (ADR-007).

The plaintext session token lives only in the httpOnly cookie; the DB stores its HMAC
hash (see security.hash_session_token). Sessions are revocable and expire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..db import Database
from ..security import (
    generate_session_token,
    hash_session_token,
    verify_password,
)


@dataclass(frozen=True)
class SessionInfo:
    id: str
    created_at: datetime


class InvalidCredentials(Exception):
    """Raised when the supplied password does not match."""


class AuthService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    def verify_password(self, password: str) -> bool:
        """Verify the single login password without opening a session.

        The MCP OAuth ``/authorize`` gate (ADR-046 §2) reuses this to authenticate the consent
        step when there is no valid PWA session, without minting a web session for a connector.
        """
        return verify_password(self._settings.api_password_hash, password)

    async def login(self, password: str, *, user_agent: str | None) -> str:
        """Verify the password and open a session. Returns the plaintext cookie token."""
        if not self.verify_password(password):
            raise InvalidCredentials

        token = generate_session_token()
        token_hash = hash_session_token(token, self._settings.session_secret)
        expires_at = datetime.now(UTC) + timedelta(days=self._settings.session_ttl_days)

        async with self._db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_sessions (token_hash, user_agent, expires_at)
                VALUES ($1, $2, $3)
                """,
                token_hash,
                user_agent,
                expires_at,
            )
        return token

    async def validate(self, token: str | None) -> SessionInfo | None:
        """Return the session for a cookie token, or None if missing/expired/revoked."""
        if not token:
            return None
        token_hash = hash_session_token(token, self._settings.session_secret)
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE auth_sessions
                   SET last_seen_at = now()
                 WHERE token_hash = $1
                   AND revoked = false
                   AND expires_at > now()
             RETURNING id, created_at
                """,
                token_hash,
            )
        if row is None:
            return None
        return SessionInfo(id=str(row["id"]), created_at=row["created_at"])

    async def logout(self, token: str | None) -> None:
        """Revoke the session bound to a cookie token (idempotent)."""
        if not token:
            return
        token_hash = hash_session_token(token, self._settings.session_secret)
        async with self._db.acquire() as conn:
            await conn.execute(
                "UPDATE auth_sessions SET revoked = true WHERE token_hash = $1",
                token_hash,
            )
