"""Persistence for the chat-distiller watermark (02-data-model §3, ADR-048 §5).

`chat_sessions` has no close/idle signal, so idle-eligibility is derived live from
``max(chat_messages.created_at)``; the **`chat_distill_state`** table holds one message-timestamp
**watermark** per distilled session. A distiller run processes only the messages *after* the
watermark (the delta), so re-runs — crash recovery, a manual `remember` then the nightly, a reopened
thread — never re-emit old turns and are a no-op with no new activity (idempotent re-distillation,
ADR-048 §5).

The service depends on the :class:`ChatDistillStore` *protocol*, not on asyncpg, so it unit-tests
against an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgChatDistillStore` is the
plain-SQL asyncpg implementation (rule 5, ADR-011), exercised by the local smoke script.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..db import Database
from .store import ChatMessageRecord, _message


@dataclass(frozen=True)
class DistillableSession:
    """A session eligible for distillation on this run: its id + the current watermark (``None`` on
    the first ever distill). ``newest_at`` is that session's most-recent message time (the value the
    watermark advances to once the run materializes its candidates)."""

    session_id: str
    watermark: datetime | None
    newest_at: datetime


@dataclass(frozen=True)
class SessionDistillState:
    """The distill state of ONE session, fetched by id for the on-demand ``remember`` path (ADR-048
    §6) regardless of idle-eligibility. ``newest_at`` is ``None`` when the session exists but has no
    messages yet (nothing to distill). The session existing at all (vs. ``session_state`` returning
    ``None``) is what separates a 404 from a skip in the endpoint."""

    session_id: str
    watermark: datetime | None
    newest_at: datetime | None


class ChatDistillStore(Protocol):
    """The chat-distiller persistence surface: which sessions are due, their delta, watermark."""

    async def distillable_sessions(
        self, *, idle_cutoff: datetime, limit: int
    ) -> list[DistillableSession]:
        """Sessions whose newest message is older than ``idle_cutoff`` (idle long enough) **and**
        that have at least one message after their watermark (new activity to distill), oldest
        activity first. Bounded by ``limit`` (one run's budget)."""
        ...

    async def delta_messages(
        self, session_id: str, *, after: datetime | None, limit: int
    ) -> list[ChatMessageRecord]:
        """The session's messages created strictly after ``after`` (all of them when ``after`` is
        ``None`` — first distill), **oldest-first**, capped at the ``limit`` **oldest** when huge —
        so a pathologically long delta is distilled in chronological batches across runs (the caller
        advances the watermark to the last message it processed), never skipping the older ones."""
        ...

    async def advance_watermark(
        self, session_id: str, *, last_message_at: datetime, run_id: str | None
    ) -> None:
        """Upsert the session's watermark to ``last_message_at`` (idempotent — a re-run with no
        newer activity leaves the delta empty and the session no longer eligible)."""
        ...

    async def session_state(self, session_id: str) -> SessionDistillState | None:
        """One session's watermark + newest-message time, by id, for the on-demand ``remember``
        path (ADR-048 §6) — no idle-eligibility filter. ``None`` when the session id is unknown (→
        404); a known session with no messages returns ``newest_at=None`` (→ skip, nothing to
        distill)."""
        ...


class PgChatDistillStore:
    """asyncpg-backed chat-distiller state — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def distillable_sessions(
        self, *, idle_cutoff: datetime, limit: int
    ) -> list[DistillableSession]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.session_id,
                       s.last_message_at AS watermark,
                       max(m.created_at) AS newest_at
                  FROM chat_messages m
                  LEFT JOIN chat_distill_state s ON s.session_id = m.session_id
                 GROUP BY m.session_id, s.last_message_at
                HAVING max(m.created_at) < $1
                   AND (s.last_message_at IS NULL OR max(m.created_at) > s.last_message_at)
                 ORDER BY max(m.created_at)
                 LIMIT $2
                """,
                idle_cutoff,
                limit,
            )
        return [
            DistillableSession(
                session_id=str(r["session_id"]),
                watermark=r["watermark"],
                newest_at=r["newest_at"],
            )
            for r in rows
        ]

    async def delta_messages(
        self, session_id: str, *, after: datetime | None, limit: int
    ) -> list[ChatMessageRecord]:
        async with self._db.acquire() as conn:
            # Oldest-first + LIMIT: a huge delta yields its OLDEST `limit` messages this run; the
            # caller advances the watermark to the last one, so the newer remainder is picked up
            # next run (a deferral, never a silent skip — ADR-048 §5 / rule 6).
            rows = await conn.fetch(
                """
                SELECT id, role, content, model, sources, created_at
                  FROM chat_messages
                 WHERE session_id = $1 AND ($2::timestamptz IS NULL OR created_at > $2)
                 ORDER BY created_at, id
                 LIMIT $3
                """,
                session_id,
                after,
                limit,
            )
        return [_message(r) for r in rows]

    async def session_state(self, session_id: str) -> SessionDistillState | None:
        async with self._db.acquire() as conn:
            # LEFT JOINs so an existing session with no watermark and/or no messages still returns a
            # row (distinguishing "unknown session" → no row → 404 from "nothing to distill" →
            # newest_at NULL → skip). Grouped on the session + its scalar watermark.
            row = await conn.fetchrow(
                """
                SELECT s.id,
                       st.last_message_at AS watermark,
                       max(m.created_at)  AS newest_at
                  FROM chat_sessions s
                  LEFT JOIN chat_distill_state st ON st.session_id = s.id
                  LEFT JOIN chat_messages m ON m.session_id = s.id
                 WHERE s.id = $1
                 GROUP BY s.id, st.last_message_at
                """,
                session_id,
            )
        if row is None:
            return None
        return SessionDistillState(
            session_id=str(row["id"]),
            watermark=row["watermark"],
            newest_at=row["newest_at"],
        )

    async def advance_watermark(
        self, session_id: str, *, last_message_at: datetime, run_id: str | None
    ) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO chat_distill_state (session_id, last_message_at, distilled_at, run_id)
                VALUES ($1, $2, now(), $3)
                ON CONFLICT (session_id) DO UPDATE
                   SET last_message_at = EXCLUDED.last_message_at,
                       distilled_at    = EXCLUDED.distilled_at,
                       run_id          = EXCLUDED.run_id
                """,
                session_id,
                last_message_at,
                run_id,
            )
