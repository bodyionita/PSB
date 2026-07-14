"""Persistence for the chat pipeline (02-data-model §3, ADR-025).

The service depends on the :class:`ChatStore` *protocol*, not on asyncpg, so it unit-tests against
an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgChatStore` is the plain-SQL
asyncpg implementation (CLAUDE.md rule 5, ADR-011), exercised by the local smoke script.

``chat_messages.model`` records the resolved model (fallback included — rule 3); ``sources`` holds
the **cited** nodes (renumbered ``[1..m]``) as jsonb, in the API source shape so the read path
(``GET /chat/sessions/{id}``, task 4) returns them verbatim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..db import Database

# Message roles persisted in chat_messages.role.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True)
class ChatSessionRecord:
    """A chat session row (chat_sessions). ``title`` is null until the best-effort titler lands
    one; ``last_model`` is the model that answered the most recent turn."""

    id: str
    title: str | None
    created_at: datetime | None
    last_model: str | None


@dataclass(frozen=True)
class ChatMessageRecord:
    """One persisted turn (chat_messages). ``sources`` is the cited-node list for assistant turns
    (empty for user turns / uncited answers), each a dict in the API source shape."""

    id: str
    role: str
    content: str
    model: str | None
    sources: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime | None = None


class ChatStore(Protocol):
    """The chat persistence surface the service relies on."""

    async def create_session(self, *, title: str | None = None) -> str:
        """Create a session and return its id (implicit-session creation, 03-api §Chat)."""
        ...

    async def get_session(self, session_id: str) -> ChatSessionRecord | None: ...

    async def list_sessions(self, limit: int) -> list[ChatSessionRecord]:
        """Sessions newest-first for the thread list (GET /chat/sessions, task 4)."""
        ...

    async def session_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[ChatMessageRecord]:
        """A session's messages oldest-first. ``limit`` keeps only the most recent N (still
        returned oldest-first) for the condense/answer window."""
        ...

    async def add_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        model: str | None = None,
        sources: list[dict[str, Any]] | None = None,
    ) -> str:
        """Append a message; returns its id."""
        ...

    async def set_title(self, session_id: str, title: str) -> None: ...

    async def set_last_model(self, session_id: str, model: str) -> None: ...


class PgChatStore:
    """asyncpg-backed chat store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_session(self, *, title: str | None = None) -> str:
        async with self._db.acquire() as conn:
            return str(
                await conn.fetchval(
                    "INSERT INTO chat_sessions (title) VALUES ($1) RETURNING id", title
                )
            )

    async def get_session(self, session_id: str) -> ChatSessionRecord | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, created_at, last_model FROM chat_sessions WHERE id = $1",
                session_id,
            )
        return _session(row) if row is not None else None

    async def list_sessions(self, limit: int) -> list[ChatSessionRecord]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, created_at, last_model
                FROM chat_sessions ORDER BY created_at DESC LIMIT $1
                """,
                limit,
            )
        return [_session(r) for r in rows]

    async def session_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[ChatMessageRecord]:
        async with self._db.acquire() as conn:
            if limit is None:
                rows = await conn.fetch(
                    """
                    SELECT id, role, content, model, sources, created_at
                    FROM chat_messages WHERE session_id = $1 ORDER BY created_at, id
                    """,
                    session_id,
                )
            else:
                # Take the newest `limit` (DESC) then flip back to chronological order so the
                # condense/answer window reads oldest-first.
                rows = await conn.fetch(
                    """
                    SELECT id, role, content, model, sources, created_at FROM (
                        SELECT id, role, content, model, sources, created_at
                        FROM chat_messages WHERE session_id = $1
                        ORDER BY created_at DESC, id DESC LIMIT $2
                    ) recent ORDER BY created_at, id
                    """,
                    session_id,
                    limit,
                )
        return [_message(r) for r in rows]

    async def add_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        model: str | None = None,
        sources: list[dict[str, Any]] | None = None,
    ) -> str:
        # sources is jsonb — asyncpg has no dict→jsonb codec, so serialize + cast (as the routing
        # store does for app_settings). NULL when there are no sources (user turns / uncited).
        payload = json.dumps(sources) if sources else None
        async with self._db.acquire() as conn:
            return str(
                await conn.fetchval(
                    """
                    INSERT INTO chat_messages (session_id, role, content, model, sources)
                    VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id
                    """,
                    session_id,
                    role,
                    content,
                    model,
                    payload,
                )
            )

    async def set_title(self, session_id: str, title: str) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                "UPDATE chat_sessions SET title = $2 WHERE id = $1", session_id, title
            )

    async def set_last_model(self, session_id: str, model: str) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                "UPDATE chat_sessions SET last_model = $2 WHERE id = $1", session_id, model
            )


def _session(row) -> ChatSessionRecord:
    return ChatSessionRecord(
        id=str(row["id"]),
        title=row["title"],
        created_at=row["created_at"],
        last_model=row["last_model"],
    )


def _message(row) -> ChatMessageRecord:
    raw = row["sources"]
    sources = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return ChatMessageRecord(
        id=str(row["id"]),
        role=row["role"],
        content=row["content"],
        model=row["model"],
        sources=list(sources),
        created_at=row["created_at"],
    )
