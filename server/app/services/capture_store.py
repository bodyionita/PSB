"""Persistence for the capture pipeline (02-data-model §3).

The pipeline depends on the :class:`CaptureStore` *protocol*, not on asyncpg, so it can be
unit-tested with an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgCaptureStore`
is the plain-SQL asyncpg implementation (CLAUDE.md rule 5); it is exercised by the local smoke
script, not the CI unit suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..db import Database

# --- Capture lifecycle statuses (02-data-model §3). ---
RECEIVED = "received"
TRANSCRIBING = "transcribing"
# Image captures derive their text (photo → vision description, M9 T3 / ADR-057 §3) between
# `received` and `organizing` — the sibling of `transcribing` for the voice leg.
DERIVING = "deriving"
ORGANIZING = "organizing"
WRITTEN = "written"
INDEXED = "indexed"
FAILED = "failed"

# Terminal states: work is done (indexed) or explicitly stopped (failed). Everything else is
# in-flight and, if found so at boot, was interrupted by a restart.
TERMINAL_STATUSES = frozenset({INDEXED, FAILED})

KIND_TEXT = "text"
KIND_VOICE = "voice"
# Ad-hoc PWA photo capture (M9 T3, ADR-057 §6): raw image kept under the media substrate, its
# vision description derived, then organized (fenced) exactly like a voice transcript.
KIND_IMAGE = "image"


@dataclass(frozen=True)
class CaptureNodeRef:
    """One of a capture's resulting nodes, **id-resolved** (M8.1 T4, ADR-054 §5 replan).

    ``CaptureRecord.node_paths`` are graph-store *paths* — projections, not identity
    (02-data-model §Identity: "paths are projections") — so a web client can't open
    ``NodePreview`` (``GET /nodes/{id}``, uuid-keyed) from a path alone. This is the read-time
    ``node_paths -> nodes.id`` join (no migration, no write) that resolves each path to its
    frontmatter uuid + a title/type hint, so the client can render a clickable ``NodeChip``
    without a follow-up round-trip. A path with no matching (or since-tombstoned) ``nodes`` row
    is simply absent here — degrades to the plain path list, never an error."""

    id: str
    store_path: str
    type: str | None
    title: str | None


@dataclass(frozen=True)
class CaptureMediaRef:
    """The media item backing an ad-hoc image capture (M9 T3, ADR-057 §6), resolved at read time
    from ``media.capture_id`` so the web can render the photo (``GET /media/{id}``) + a derivation-
    status badge straight off the capture. ``None`` for text/voice/mcp/chat captures (no media
    row). ``status`` is the derivation lifecycle (``pending``/``derived``/``unavailable``)."""

    id: str
    kind: str
    status: str


@dataclass
class CaptureRecord:
    id: str
    kind: str
    status: str
    raw_text: str | None = None
    audio_path: str | None = None
    node_paths: list[str] = field(default_factory=list)
    # Id-resolved projection of `node_paths` (M8.1 T4, ADR-054 §5 replan) — see `CaptureNodeRef`.
    # Populated by `get`/`list_recent`'s read-time join; empty on a freshly-created record (no
    # nodes yet) and never written back to `captures` (derived, not stored).
    node_refs: list[CaptureNodeRef] = field(default_factory=list)
    # The backing media item for an image capture (M9 T3) — resolved by `get`/`list_recent`'s
    # read-time `media.capture_id` join; None for non-image captures. Derived, not stored here.
    media_ref: CaptureMediaRef | None = None
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

    async def get(self, capture_id: str) -> CaptureRecord | None:
        """A single capture, its ``node_refs`` (M8.1 T4) resolved via the ``node_paths -> nodes.id``
        read-time join (a path with no live node row is simply omitted)."""
        ...

    async def list_recent(self, limit: int) -> list[CaptureRecord]:
        """Newest-first, ``node_refs``-resolved like :meth:`get`."""
        ...

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

    async def set_created_at(self, capture_id: str, created_at: datetime) -> None:
        """Correct a capture's recorded-at — the ADR-056 §5 anchor edit. The stored anchor is data
        (never wall-clock), so overwriting it makes a subsequent reorganize re-resolve every
        relative date against the corrected time (reprocess-deterministic)."""
        ...

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

# The M8.1 T4 read-time `node_paths -> nodes.id` join (ADR-054 §5 replan; no migration, no write —
# `nodes` is the derived index, `captures.node_paths` stays the store-path projection). A LATERAL
# subquery per capture row, jsonb-aggregating the resolved refs in `node_paths` order
# (`array_position`) so the client can render them in the order the organizer wrote them; a path
# with no live `nodes` row (not yet indexed, or tombstoned) is simply absent, never an error.
# `jsonb_agg` over zero matching rows returns SQL NULL, decoded to `[]` by `_node_refs`.
_NODE_REFS_JOIN = """
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(
                 jsonb_build_object('id', n.id, 'store_path', n.store_path,
                                     'type', n.type, 'title', n.title)
                 ORDER BY array_position(c.node_paths, n.store_path)
               ) AS refs
          FROM nodes n
         WHERE n.store_path = ANY(c.node_paths)
    ) node_refs ON true
"""

# The M9 T3 read-time `media.capture_id -> media` join: the one media item backing an ad-hoc image
# capture (1:1; LIMIT 1 is a defensive cap), as a jsonb object so the web renders the photo +
# derivation-status badge off the capture without a second round-trip. NULL for a capture with no
# media (text/voice/mcp/chat) — decoded to None by `_media_ref`.
_MEDIA_REF_JOIN = """
    LEFT JOIN LATERAL (
        SELECT jsonb_build_object('id', m.id, 'kind', m.kind, 'status', m.status) AS media
          FROM media m
         WHERE m.capture_id = c.id
         ORDER BY m.created_at ASC
         LIMIT 1
    ) media_ref ON true
"""


def _media_ref(raw: object) -> CaptureMediaRef | None:
    # asyncpg returns jsonb as text by default; tolerate both (mirrors `_node_refs`). No media row
    # for this capture ⇒ the LEFT JOIN yields SQL NULL ⇒ None.
    if raw is None:
        return None
    item = json.loads(raw) if isinstance(raw, str) else raw
    if not item:
        return None
    return CaptureMediaRef(id=str(item["id"]), kind=item["kind"], status=item["status"])


def _node_refs(raw: object) -> list[CaptureNodeRef]:
    # asyncpg returns jsonb as text by default; tolerate both text and an already-decoded list
    # (mirrors `agent_runs._details` / `graph_health`'s jsonb decode convention).
    if raw is None:
        return []
    items = json.loads(raw) if isinstance(raw, str) else raw
    return [
        CaptureNodeRef(
            id=str(item["id"]),
            store_path=item["store_path"],
            type=item.get("type"),
            title=item.get("title"),
        )
        for item in items
    ]


def _record(
    row,
    *,
    node_refs: list[CaptureNodeRef] | None = None,
    media_ref: CaptureMediaRef | None = None,
) -> CaptureRecord:
    return CaptureRecord(
        id=str(row["id"]),
        kind=row["kind"],
        status=row["status"],
        raw_text=row["raw_text"],
        audio_path=row["audio_path"],
        node_paths=list(row["node_paths"] or []),
        node_refs=node_refs or [],
        media_ref=media_ref,
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
            row = await conn.fetchrow(
                f"""
                SELECT {_COLUMNS}, node_refs.refs AS node_refs, media_ref.media AS media_ref
                  FROM captures c
                {_NODE_REFS_JOIN}
                {_MEDIA_REF_JOIN}
                 WHERE c.id = $1
                """,
                capture_id,
            )
        if row is None:
            return None
        return _record(
            row, node_refs=_node_refs(row["node_refs"]), media_ref=_media_ref(row["media_ref"])
        )

    async def list_recent(self, limit: int) -> list[CaptureRecord]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS}, node_refs.refs AS node_refs, media_ref.media AS media_ref
                  FROM captures c
                {_NODE_REFS_JOIN}
                {_MEDIA_REF_JOIN}
                 ORDER BY c.created_at DESC
                 LIMIT $1
                """,
                limit,
            )
        return [
            _record(r, node_refs=_node_refs(r["node_refs"]), media_ref=_media_ref(r["media_ref"]))
            for r in rows
        ]

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

    async def set_created_at(self, capture_id: str, created_at: datetime) -> None:
        await self._set(capture_id, "created_at = $2", created_at)

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
