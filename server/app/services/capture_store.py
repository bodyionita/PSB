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
# Pre-submit composite draft (M9.6 T1, ADR-061 §3): parts are attached incrementally and the draft
# is resumable across app-close. A draft is NOT in-flight — the boot orphan-sweep skips it, and a
# separate 7-day GC reclaims abandoned ones.
DRAFT = "draft"
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
# Statuses the boot orphan-sweep must NOT flip to `failed` (ADR-061 §9): the terminal states plus
# `draft` (a draft is intentionally open, not a crashed in-flight run — it is reclaimed by the 7-day
# draft GC, never swept).
NON_SWEEPABLE_STATUSES = TERMINAL_STATUSES | {DRAFT}

KIND_TEXT = "text"
KIND_VOICE = "voice"
# Ad-hoc PWA photo capture (M9 T3, ADR-057 §6): raw image kept under the media substrate, its
# vision description derived, then organized (fenced) exactly like a voice transcript.
KIND_IMAGE = "image"
# Composite multi-part capture (M9.6, ADR-061 §2): an optional typed text body + 0..N photos +
# <=1 voice, composed on a draft and organized in one blended pass. The single-modality kinds are
# the degenerate cases; a composite's node `source` is `web` (it has no single modality).
KIND_COMPOSITE = "composite"


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
    """One media item on a capture (M9 T3/T4 → M9.6 T4, ADR-057 §6 / ADR-060 §5 / ADR-061 §11),
    resolved at read time from ``media.capture_id`` so the web can render the photo / voice player
    (``GET /media/{id}``) + a derivation-status badge straight off the capture. A capture carries a
    **list** of these (M9.6: 0..N photos + <=1 voice), ordered by ``part_ordinal``; empty for
    text/mcp/chat captures. ``kind`` is ``photo``/``voice``; ``status`` is the derivation lifecycle
    (``pending``/``derived``/``unavailable``)."""

    id: str
    kind: str
    status: str
    part_ordinal: int | None = None


@dataclass
class CaptureRecord:
    id: str
    kind: str
    status: str
    raw_text: str | None = None
    # The person's typed words on a composite capture (M9.6 T1, ADR-061 §5) — never-lose + the
    # reassembly source. `raw_text` stays the cached assembled organize/replay source; `text_body`
    # is one editable field on the draft, NULL for single-modality captures.
    text_body: str | None = None
    audio_path: str | None = None
    node_paths: list[str] = field(default_factory=list)
    # Id-resolved projection of `node_paths` (M8.1 T4, ADR-054 §5 replan) — see `CaptureNodeRef`.
    # Populated by `get`/`list_recent`'s read-time join; empty on a freshly-created record (no
    # nodes yet) and never written back to `captures` (derived, not stored).
    node_refs: list[CaptureNodeRef] = field(default_factory=list)
    # The capture's media parts (M9.6 T4, ADR-061 §11 — singular → list) — resolved by `get`/
    # `list_recent`'s read-time `media.capture_id` join, ordered by `part_ordinal`; empty for
    # text/mcp/chat captures. Derived, not stored here.
    media_refs: list[CaptureMediaRef] = field(default_factory=list)
    # The capture's most recent processing `agent_runs` id (M9.6 T4, ADR-061 §10 — the Activity-tab
    # deep-link), resolved read-time from `agent_runs.details->>'capture_id'`; None until a run
    # exists. Derived, not stored on the row.
    run_id: str | None = None
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
        text_body: str | None = None,
        audio_path: str | None = None,
        created_at: datetime | None = None,
        source: str | None = None,
        source_ref: str | None = None,
    ) -> CaptureRecord: ...

    async def get_active_draft(self) -> CaptureRecord | None:
        """The single open composite draft, if any (M9.6 T1, ADR-061 §3 — one active draft). The
        partial unique index guarantees at most one; opening the compose screen resumes it."""
        ...

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

    async def set_text_body(self, capture_id: str, text_body: str) -> None:
        """Edit a composite draft's typed text body (M9.6 T1, ADR-061 §3/§5)."""
        ...

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
        """Mark every non-terminal, non-draft capture as failed (boot recovery). A ``draft`` is
        intentionally open, not a crashed run, so the sweep skips it (ADR-061 §9). Returns the
        count."""
        ...

    async def delete(self, capture_id: str) -> None:
        """Hard-delete a capture row (M9.6 T1, ADR-061 §3/§9 — discard / draft GC). Its ``media``
        rows cascade (fk ``ON DELETE CASCADE``); the caller removes the raw files first."""
        ...

    async def list_drafts_created_before(self, cutoff: datetime) -> list[CaptureRecord]:
        """Open drafts created before ``cutoff`` — the 7-day draft GC scan (ADR-061 §9)."""
        ...


_COLUMNS = (
    "id, kind, status, raw_text, text_body, audio_path, node_paths, "
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

# The M9 T3/T4 → M9.6 T4 read-time `media.capture_id -> media` join: the capture's media items as an
# ordered jsonb ARRAY (composite: 0..N photos + <=1 voice), ordered by `part_ordinal` (legacy
# single-part media have NULL ordinal → fall back to `created_at`), so the web renders the list
# + per-part derivation badges off the capture without a second round-trip. NULL (→ `[]`) for a
# capture with no media (text/mcp/chat) — decoded by `_media_refs`.
_MEDIA_REF_JOIN = """
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(
                 jsonb_build_object('id', m.id, 'kind', m.kind, 'status', m.status,
                                    'part_ordinal', m.part_ordinal)
                 ORDER BY m.part_ordinal ASC NULLS LAST, m.created_at ASC, m.id
               ) AS media
          FROM media m
         WHERE m.capture_id = c.id
    ) media_ref ON true
"""

# The M9.6 T4 read-time capture → Activity-run deep-link (ADR-061 §10): the capture's most recent
# processing `agent_runs` id, found via the `capture_id` the pipeline stamps into `details` (every
# `_process`/reorganize/reprocess interaction). NULL (→ None) until a run exists. Newest run wins so
# the link always points at the latest processing pass.
_RUN_REF_JOIN = """
    LEFT JOIN LATERAL (
        SELECT r.id AS run_id
          FROM agent_runs r
         WHERE r.agent = 'capture' AND r.details->>'capture_id' = c.id::text
         ORDER BY r.started_at DESC
         LIMIT 1
    ) run_ref ON true
"""


def _media_refs(raw: object) -> list[CaptureMediaRef]:
    # asyncpg returns jsonb as text by default; tolerate both (mirrors `_node_refs`). No media rows
    # ⇒ `jsonb_agg` over zero rows is SQL NULL ⇒ [].
    if raw is None:
        return []
    items = json.loads(raw) if isinstance(raw, str) else raw
    return [
        CaptureMediaRef(
            id=str(item["id"]),
            kind=item["kind"],
            status=item["status"],
            part_ordinal=item.get("part_ordinal"),
        )
        for item in items
    ]


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
    media_refs: list[CaptureMediaRef] | None = None,
    run_id: str | None = None,
) -> CaptureRecord:
    return CaptureRecord(
        id=str(row["id"]),
        kind=row["kind"],
        status=row["status"],
        raw_text=row["raw_text"],
        text_body=row["text_body"],
        audio_path=row["audio_path"],
        node_paths=list(row["node_paths"] or []),
        node_refs=node_refs or [],
        media_refs=media_refs or [],
        run_id=run_id,
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
        text_body: str | None = None,
        audio_path: str | None = None,
        created_at: datetime | None = None,
        source: str | None = None,
        source_ref: str | None = None,
    ) -> CaptureRecord:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO captures
                    (id, kind, status, raw_text, text_body, audio_path, created_at, source,
                     source_ref)
                VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, now()), $8, $9)
                RETURNING {_COLUMNS}
                """,
                capture_id,
                kind,
                status,
                raw_text,
                text_body,
                audio_path,
                created_at,
                source,
                source_ref,
            )
        return _record(row)

    async def get_active_draft(self) -> CaptureRecord | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLUMNS} FROM captures WHERE status = $1 "
                f"ORDER BY created_at DESC LIMIT 1",
                DRAFT,
            )
        return _record(row) if row is not None else None

    async def get(self, capture_id: str) -> CaptureRecord | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_COLUMNS}, node_refs.refs AS node_refs, media_ref.media AS media_refs,
                       run_ref.run_id AS run_id
                  FROM captures c
                {_NODE_REFS_JOIN}
                {_MEDIA_REF_JOIN}
                {_RUN_REF_JOIN}
                 WHERE c.id = $1
                """,
                capture_id,
            )
        if row is None:
            return None
        return _record(
            row,
            node_refs=_node_refs(row["node_refs"]),
            media_refs=_media_refs(row["media_refs"]),
            run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        )

    async def list_recent(self, limit: int) -> list[CaptureRecord]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS}, node_refs.refs AS node_refs, media_ref.media AS media_refs,
                       run_ref.run_id AS run_id
                  FROM captures c
                {_NODE_REFS_JOIN}
                {_MEDIA_REF_JOIN}
                {_RUN_REF_JOIN}
                 ORDER BY c.created_at DESC
                 LIMIT $1
                """,
                limit,
            )
        return [
            _record(
                r,
                node_refs=_node_refs(r["node_refs"]),
                media_refs=_media_refs(r["media_refs"]),
                run_id=str(r["run_id"]) if r["run_id"] is not None else None,
            )
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

    async def set_text_body(self, capture_id: str, text_body: str) -> None:
        await self._set(capture_id, "text_body = $2", text_body)

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
                # Excludes the terminal states AND `draft` (ADR-061 §9): a draft is intentionally
                # open, not a crashed in-flight run — it is reclaimed by the 7-day GC, never swept.
                list(NON_SWEEPABLE_STATUSES),
            )
        # asyncpg returns e.g. "UPDATE 3"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def delete(self, capture_id: str) -> None:
        async with self._db.acquire() as conn:
            await conn.execute("DELETE FROM captures WHERE id = $1", capture_id)

    async def list_drafts_created_before(self, cutoff: datetime) -> list[CaptureRecord]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM captures "
                f"WHERE status = $1 AND created_at < $2 ORDER BY created_at ASC",
                DRAFT,
                cutoff,
            )
        return [_record(r) for r in rows]
