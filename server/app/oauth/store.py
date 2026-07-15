"""OAuth AS persistence — clients, single-use auth codes, opaque tokens (M5 task 3, ADR-046 §2).

Plain SQL over asyncpg, no ORM (rule 5, ADR-011). The service (flow orchestration + crypto)
unit-tests against the :class:`OAuthStore` protocol with a fake; the real ``PgOAuthStore`` is
exercised by the real-PG smoke (the un-fakeable atomic single-use consume + the revoke-all sweep).

Only hashes are ever stored — codes and tokens are credentials, so the plaintext leaves the server
exactly once (code in the redirect, tokens in the ``/token`` response), same discipline as
``auth_sessions`` (``security.hash_session_token``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..db import Database


@dataclass(frozen=True)
class ClientRecord:
    client_id: str
    client_secret_hash: str | None
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class AuthCodeRecord:
    """A consumed authorization code's bound parameters (returned by the single-use consume)."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    scope: str
    resource: str | None


@dataclass(frozen=True)
class TokenRecord:
    id: str
    client_id: str
    kind: str
    scope: str
    resource: str | None
    expires_at: datetime
    revoked_at: datetime | None


class OAuthStore(Protocol):
    async def create_client(
        self, *, client_id: str, client_secret_hash: str | None, metadata: dict[str, Any]
    ) -> None: ...

    async def get_client(self, client_id: str) -> ClientRecord | None: ...

    async def create_code(
        self,
        *,
        code_hash: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        scope: str,
        resource: str | None,
        expires_at: datetime,
    ) -> None: ...

    async def consume_code(self, code_hash: str) -> AuthCodeRecord | None: ...

    async def consumed_code_client(self, code_hash: str) -> str | None: ...

    async def create_token(
        self,
        *,
        client_id: str,
        token_hash: str,
        kind: str,
        scope: str,
        resource: str | None,
        expires_at: datetime,
    ) -> str: ...

    async def get_token(self, token_hash: str) -> TokenRecord | None: ...

    async def touch_token(self, token_hash: str) -> None: ...

    async def revoke_token(self, token_hash: str) -> int: ...

    async def revoke_client_tokens(self, client_id: str) -> int: ...

    async def invalidate_all_codes(self) -> int: ...

    async def revoke_all(self) -> int: ...


class PgOAuthStore:
    """asyncpg-backed OAuth store — plain SQL (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_client(
        self, *, client_id: str, client_secret_hash: str | None, metadata: dict[str, Any]
    ) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO mcp_oauth_clients (client_id, client_secret_hash, metadata)
                VALUES ($1, $2, $3::jsonb)
                """,
                client_id,
                client_secret_hash,
                json.dumps(metadata),
            )

    async def get_client(self, client_id: str) -> ClientRecord | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT client_id, client_secret_hash, metadata, created_at "
                "FROM mcp_oauth_clients WHERE client_id = $1",
                client_id,
            )
        if row is None:
            return None
        return ClientRecord(
            client_id=row["client_id"],
            client_secret_hash=row["client_secret_hash"],
            metadata=_decode_json_obj(row["metadata"]),
            created_at=row["created_at"],
        )

    async def create_code(
        self,
        *,
        code_hash: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        scope: str,
        resource: str | None,
        expires_at: datetime,
    ) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO mcp_auth_codes
                    (code_hash, client_id, redirect_uri, code_challenge, code_challenge_method,
                     scope, resource, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                code_hash,
                client_id,
                redirect_uri,
                code_challenge,
                code_challenge_method,
                scope,
                resource,
                expires_at,
            )

    async def consume_code(self, code_hash: str) -> AuthCodeRecord | None:
        # Atomic single-use: the row is returned only if it was still unconsumed AND unexpired at
        # this instant — a concurrent double-exchange loses the race and gets None (invalid_grant).
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                """
                UPDATE mcp_auth_codes
                   SET consumed_at = now()
                 WHERE code_hash = $1
                   AND consumed_at IS NULL
                   AND expires_at > now()
             RETURNING client_id, redirect_uri, code_challenge, code_challenge_method,
                       scope, resource
                """,
                code_hash,
            )
        if row is None:
            return None
        return AuthCodeRecord(
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            code_challenge=row["code_challenge"],
            code_challenge_method=row["code_challenge_method"],
            scope=row["scope"],
            resource=row["resource"],
        )

    async def consumed_code_client(self, code_hash: str) -> str | None:
        """Return the owning ``client_id`` iff the code exists **and was already consumed**.

        Used for replay detection: a consume miss that finds an already-spent code means the code
        is being replayed → the service revokes the client's tokens (OAuth 2.1 §4.1.2 replay
        guard). An expired-but-never-consumed code returns ``None`` (no tokens ever issued)."""
        async with self._db.acquire() as conn:
            return await conn.fetchval(
                "SELECT client_id FROM mcp_auth_codes "
                "WHERE code_hash = $1 AND consumed_at IS NOT NULL",
                code_hash,
            )

    async def create_token(
        self,
        *,
        client_id: str,
        token_hash: str,
        kind: str,
        scope: str,
        resource: str | None,
        expires_at: datetime,
    ) -> str:
        async with self._db.transaction() as conn:
            token_id = await conn.fetchval(
                """
                INSERT INTO mcp_tokens (client_id, token_hash, kind, scope, resource, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                client_id,
                token_hash,
                kind,
                scope,
                resource,
                expires_at,
            )
        return str(token_id)

    async def get_token(self, token_hash: str) -> TokenRecord | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, client_id, kind, scope, resource, expires_at, revoked_at "
                "FROM mcp_tokens WHERE token_hash = $1",
                token_hash,
            )
        if row is None:
            return None
        return TokenRecord(
            id=str(row["id"]),
            client_id=row["client_id"],
            kind=row["kind"],
            scope=row["scope"],
            resource=row["resource"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    async def touch_token(self, token_hash: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE mcp_tokens SET last_used_at = now() WHERE token_hash = $1", token_hash
            )

    async def revoke_token(self, token_hash: str) -> int:
        """Revoke one token; returns the affected-row count so the caller can detect that it lost a
        race to a concurrent revocation (refresh rotation reuse-detection, ADR-046 §2)."""
        async with self._db.transaction() as conn:
            result = await conn.execute(
                "UPDATE mcp_tokens SET revoked_at = now() "
                "WHERE token_hash = $1 AND revoked_at IS NULL",
                token_hash,
            )
        return _rowcount(result)

    async def revoke_client_tokens(self, client_id: str) -> int:
        async with self._db.transaction() as conn:
            result = await conn.execute(
                "UPDATE mcp_tokens SET revoked_at = now() "
                "WHERE client_id = $1 AND revoked_at IS NULL",
                client_id,
            )
        return _rowcount(result)

    async def invalidate_all_codes(self) -> int:
        """Consume every outstanding (unexchanged) authorization code — part of revoke-all, so the
        switch is *total*: a code issued but not yet redeemed can't still mint tokens afterwards."""
        async with self._db.transaction() as conn:
            result = await conn.execute(
                "UPDATE mcp_auth_codes SET consumed_at = now() WHERE consumed_at IS NULL"
            )
        return _rowcount(result)

    async def revoke_all(self) -> int:
        """The M5 revoke-all switch (ADR-046 §2): flag every live token in one UPDATE."""
        async with self._db.transaction() as conn:
            result = await conn.execute(
                "UPDATE mcp_tokens SET revoked_at = now() WHERE revoked_at IS NULL"
            )
        return _rowcount(result)


def _decode_json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _rowcount(command_tag: str) -> int:
    # asyncpg returns e.g. "UPDATE 3"; the trailing integer is the affected-row count.
    try:
        return int(command_tag.split()[-1])
    except (ValueError, IndexError):
        return 0
