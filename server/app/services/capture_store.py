"""Persistence for the capture pipeline (02-data-model §3).

The pipeline depends on the :class:`CaptureStore` *protocol*, not on asyncpg, so it can be
unit-tested with an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgCaptureStore`
is the plain-SQL asyncpg implementation (CLAUDE.md rule 5); it is exercised by the local smoke
script, not the CI unit suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..db import Database

# --- Capture lifecycle statuses (02-data-model §3). ---
RECEIVED = "received"
TRANSCRIBING = "transcribing"
ORGANIZING = "organizing"
WRITTEN = "written"
INDEXED = "indexed"
FAILED = "failed"

# Terminal states: work is done (indexed) or explicitly stopped (failed). Everything else is
# in-flight and, if found so at boot, was interrupted by a restart.
TERMINAL_STATUSES = frozenset({INDEXED, FAILED})

KIND_TEXT = "text"
KIND_VOICE = "voice"


@dataclass
class CaptureRecord:
    id: str
    kind: str
    status: str
    raw_text: str | None = None
    audio_path: str | None = None
    node_paths: list[str] = field(default_factory=list)
    follow_up_question: str | None = None
    follow_up_answer: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # The capture's origin surface (ADR-046 §4): `mcp` for MCP-tool captures, `chat` for a
    # chat-distilled memory (ADR-048 §1); NULL for the web surfaces, which fall back to the capture
    # kind (`text`/`voice`) as the node source.
    source: str | None = None
    # Opaque origin locator (mirrors `nodes.source_ref`, 02-data-model): the chat-session id for a
    # `source=chat` capture (ADR-048 §1), so the chat→capture→node chain is traceable. NULL for the
    # web/voice/MCP captures.
    source_ref: str | None = None
    # One-tap-remove tombstone (ADR-048 §11, M6 task 4): non-null once a chat-distilled node is
    # removed (its file git-rm'd + `nodes`/`chunks`/`edges` deleted). Replay-excluded — `reprocess-
    # all` skips a tombstoned capture so a removed memory can't resurrect. NULL for live captures.
    removed_at: datetime | None = None


class CaptureStore(Protocol):
    """The capture persistence surface the pipeline relies on."""

    async def create(
        self,
        *,
        capture_id: str,
        kind: str,
        status: str,
        raw_text: str | None = None,
        audio_path: str | None = None,
        created_at: datetime | None = None,
        source: str | None = None,
        source_ref: str | None = None,
    ) -> CaptureRecord: ...

    async def get(self, capture_id: str) -> CaptureRecord | None: ...

    async def list_recent(self, limit: int) -> list[CaptureRecord]: ...

    async def list_inbox_materialized(self, *, folder: str, limit: int) -> list[CaptureRecord]:
        """Captures still materialized as an ``inbox/`` fallback — any ``node_paths`` element under
        ``<folder>/`` — and NOT one-tap-removed (``removed_at IS NULL``). The nightly inbox drainer
        (ADR-048 §10) re-organizes these; oldest-first + ``limit`` bound one run. Status-agnostic: a
        prior drain may have re-marked a still-unresolvable capture ``failed`` while keeping its
        inbox node, and it must stay eligible for the next night's retry."""
        ...

    async def mark_status(self, capture_id: str, status: str) -> None: ...

    async def mark_failed(self, capture_id: str, error: str) -> None: ...

    async def set_raw_text(self, capture_id: str, raw_text: str) -> None: ...

    async def set_node_paths(self, capture_id: str, node_paths: list[str]) -> None: ...

    async def set_follow_up_question(self, capture_id: str, question: str) -> None: ...

    async def set_follow_up_answer(self, capture_id: str, answer: str) -> None: ...

    async def reset_for_retry(self, capture_id: str) -> None:
        """Clear the failure and put the capture back in-flight (``received``, no error)."""
        ...

    async def sweep_orphans(self, error: str) -> int:
        """Mark every non-terminal capture as failed (boot recovery). Returns the count."""
        ...


_COLUMNS = (
    "id, kind, status, raw_text, audio_path, node_paths, "
    "follow_up_question, follow_up_answer, error, created_at, updated_at, source, source_ref, "
    "removed_at"
)


def _record(row) -> CaptureRecord:
    return CaptureRecord(
        id=str(row["id"]),
        kind=row["kind"],
        status=row["status"],
        raw_text=row["raw_text"],
        audio_path=row["audio_path"],
        node_paths=list(row["node_paths"] or []),
        follow_up_question=row["follow_up_question"],
        follow_up_answer=row["follow_up_answer"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source=row["source"],
        source_ref=row["source_ref"],
        removed_at=row["removed_at"],
    )


class PgCaptureStore:
    """asyncpg-backed capture store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        capture_id: str,
        kind: str,
        status: str,
        raw_text: str | None = None,
        audio_path: str | None = None,
        created_at: datetime | None = None,
        source: str | None = None,
        source_ref: str | None = None,
    ) -> CaptureRecord:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO captures
                    (id, kind, status, raw_text, audio_path, created_at, source, source_ref)
                VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()), $7, $8)
                RETURNING {_COLUMNS}
                """,
                capture_id,
                kind,
                status,
                raw_text,
                audio_path,
                created_at,
                source,
                source_ref,
            )
        return _record(row)

    async def get(self, capture_id: str) -> CaptureRecord | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM captures WHERE id = $1", capture_id)
        return _record(row) if row is not None else None

    async def list_recent(self, limit: int) -> list[CaptureRecord]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM captures ORDER BY created_at DESC LIMIT $1", limit
            )
        return [_record(r) for r in rows]

    async def list_inbox_materialized(self, *, folder: str, limit: int) -> list[CaptureRecord]:
        # `EXISTS (unnest … LIKE folder/%)`: any node_path under the inbox folder marks an
        # organize-fallback capture. `removed_at IS NULL` excludes one-tap-removed captures (the
        # same replay exclusion reprocess applies). Oldest-first so the longest-waiting drain first.
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS} FROM captures
                 WHERE removed_at IS NULL
                   AND EXISTS (
                       SELECT 1 FROM unnest(node_paths) AS p
                        WHERE p LIKE $1 || '/%'
                   )
                 ORDER BY created_at ASC
                 LIMIT $2
                """,
                folder,
                limit,
            )
        return [_record(r) for r in rows]

    async def _set(self, capture_id: str, assignment: str, *values) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                f"UPDATE captures SET {assignment}, updated_at = now() WHERE id = $1",
                capture_id,
                *values,
            )

    async def mark_status(self, capture_id: str, status: str) -> None:
        await self._set(capture_id, "status = $2", status)

    async def mark_failed(self, capture_id: str, error: str) -> None:
        await self._set(capture_id, "status = $2, error = $3", FAILED, error)

    async def set_raw_text(self, capture_id: str, raw_text: str) -> None:
        await self._set(capture_id, "raw_text = $2", raw_text)

    async def set_node_paths(self, capture_id: str, node_paths: list[str]) -> None:
        await self._set(capture_id, "node_paths = $2", node_paths)

    async def set_follow_up_question(self, capture_id: str, question: str) -> None:
        await self._set(capture_id, "follow_up_question = $2", question)

    async def set_follow_up_answer(self, capture_id: str, answer: str) -> None:
        await self._set(capture_id, "follow_up_answer = $2", answer)

    async def reset_for_retry(self, capture_id: str) -> None:
        await self._set(capture_id, "status = $2, error = NULL", RECEIVED)

    async def sweep_orphans(self, error: str) -> int:
        async with self._db.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE captures
                   SET status = $1, error = $2, updated_at = now()
                 WHERE status <> ALL($3::text[])
                """,
                FAILED,
                error,
                list(TERMINAL_STATUSES),
            )
        # asyncpg returns e.g. "UPDATE 3"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0
