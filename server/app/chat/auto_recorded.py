"""The chat-scoped "recently auto-recorded" audit list + one-tap remove (M6 task 4, ADR-048 §11/12).

The trust loop for stance-gated auto-ingestion (ADR-029): the distiller endorses memories overnight
*without* a per-item human decision, so the user needs to **see what was auto-added and undo any**.
This module owns both halves:

* **The audit list** (``GET /chat/auto-recorded``) — auto-endorsed chat memories, newest first. Its
  backing registry is ``chat_auto_recorded``: the distiller's endorsed branch records one row per
  auto-endorsed candidate (its ``source=chat`` capture id + the coarse salience tag). A row's
  *existence* is what marks a memory as auto-recorded — an **agree-from-review** memory materializes
  the same ``source=chat`` capture but writes **no** row, so it's user-vetted and stays off this
  surface (ADR-048 §11: remove is auto-endorsed only; general node removal = backlog).

* **One-tap remove** (``POST /chat/auto-recorded/{id}/remove``) — soft-delete a chat-distilled node:
  git-rm the node file(s) + DB-delete (``nodes``/``chunks``/``edges``) + **tombstone** the backing
  capture (``captures.removed_at``) so ``reprocess-all`` can't resurrect it (ADR-048 §11). Shared
  **entity hubs** the capture minted are preserved (ADR-038) — deleting a hub would dangle every
  other node's edge into it; only the content nodes go.

Depends on narrow protocols (capture lookup, node-delete, the registry) so it unit-tests against
fakes (no live DB — 08 testing policy). ``NodeWriter`` / ``StoreBackup`` are the same concrete
collaborators the capture pipeline uses (the store is truth — rule 1).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..config import Settings
from ..graph.node_writer import NodeWriter
from ..services.capture_store import CaptureRecord
from ..services.store_backup import StoreBackup
from ..vocab.service import VocabularyProvider, effective_vocabulary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoRecordedItem:
    """One row of the audit list (``GET /chat/auto-recorded``, 03-api §Chat / ADR-048 §12).

    ``node_paths`` are the capture's materialized store paths (empty until the background organize
    lands); ``title`` is the primary content node's title (``None`` until organized / on an inbox
    fallback); ``snippet`` is a preview of the endorsed statement (the capture raw); ``source_ref``
    is the originating chat-session id; ``salience`` is the distiller's coarse triage tag."""

    capture_id: str
    node_paths: list[str]
    title: str | None
    snippet: str
    salience: str | None
    source_ref: str | None
    created_at: datetime | None


class AutoRecordedStore(Protocol):
    """Persistence for the audit registry + the remove tombstone (plain SQL, ADR-011)."""

    async def record(self, capture_id: str, *, salience: str | None) -> None:
        """Register an auto-endorsed capture in the audit list (idempotent — a re-distill of the
        same deterministic capture id is a no-op, rule 6)."""
        ...

    async def is_recorded(self, capture_id: str) -> bool:
        """Whether ``capture_id`` is an auto-recorded item (has a ``chat_auto_recorded`` row) — the
        gate that keeps remove auto-endorsed-only (agree-from-review returns ``False``)."""
        ...

    async def tombstone(self, capture_id: str) -> bool:
        """Stamp ``captures.removed_at`` (soft-delete, replay-excluded). Returns whether a *live*
        row was tombstoned (``False`` if already removed / gone) — scoped ``WHERE removed_at IS
        NULL`` so a double-remove is a no-op."""
        ...

    async def list_recent(
        self, limit: int, *, entity_types: list[str]
    ) -> list[AutoRecordedItem]:
        """The audit list: auto-recorded, non-removed captures newest-first, joined to their primary
        content node for a title. ``entity_types`` are the hub folders to skip when picking the
        title node (a minted hub isn't the memory)."""
        ...


class CaptureLookup(Protocol):
    """The one capture read the remove op needs (its ``node_paths`` + tombstone state)."""

    async def get(self, capture_id: str) -> CaptureRecord | None: ...


class NodeDeleteStore(Protocol):
    """The index delete the remove op needs — DB rows for the removed node paths
    (``chunks``/``edges`` cascade both directions, ADR-026)."""

    async def delete_nodes(self, store_paths: list[str]) -> int: ...


class AutoRecordNotFound(Exception):
    """Remove was requested for an id that is not a live auto-recorded item (unknown, already
    removed, or not auto-endorsed) → ``404`` (03-api §Chat)."""


class AutoRecordedService:
    """Owns the audit list + the one-tap-remove op (ADR-048 §11/§12)."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: AutoRecordedStore,
        captures: CaptureLookup,
        index_store: NodeDeleteStore,
        node_writer: NodeWriter,
        store_backup: StoreBackup,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._captures = captures
        self._index = index_store
        self._writer = node_writer
        self._backup = store_backup
        # Effective entity-like types (seeds ∪ approved additions) — the hub folders remove must
        # preserve (ADR-038). None ⇒ seed-only fallback (tests without governed vocab).
        self._vocab = vocab

    async def list_recent(self, limit: int | None = None) -> list[AutoRecordedItem]:
        """The chat-scoped audit list (``GET /chat/auto-recorded``), bounded by config (rule 9)."""
        capped = min(limit or self._settings.chat_auto_recorded_list_max,
                     self._settings.chat_auto_recorded_list_max)
        entity_types = list(
            (await effective_vocabulary(self._vocab, self._settings)).entity_like_types
        )
        return await self._store.list_recent(max(1, capped), entity_types=entity_types)

    async def remove(self, capture_id: str) -> None:
        """One-tap remove of an auto-endorsed chat memory (``POST …/remove``, ADR-048 §11).

        git-rm the content node file(s) + DB-delete their rows + tombstone the capture (so
        ``reprocess-all`` skips it). Raises :class:`AutoRecordNotFound` (→404) for an id that is not
        a live auto-recorded item. Idempotent + self-healing on a retry: the DB delete targets the
        capture's **content paths** (not merely the files this call unlinked) and the tombstone is
        stamped **last**, so a failure before it leaves the capture live (``removed_at`` NULL) and a
        re-run redoes the file unlink (missing files skipped) *and* the DB delete (a no-op on
        already-gone rows) before tombstoning — never a half-removed, unretryable state.
        """
        record = await self._captures.get(capture_id)
        if record is None or record.removed_at is not None:
            raise AutoRecordNotFound(capture_id)
        if not await self._store.is_recorded(capture_id):
            # Not an auto-endorsed item (unknown, or an agree-from-review capture — user-vetted,
            # general removal stays backlog, ADR-048 §11).
            raise AutoRecordNotFound(capture_id)

        # Preserve shared entity hubs (ADR-038): a hub this capture minted is substrate owned by the
        # entity lifecycle, not this memory — deleting it would dangle every other node's edge into
        # it. The content paths (node_paths minus hub folders) are what we git-rm + DB-delete; the
        # folder=type predicate mirrors `NodeWriter.remove_nodes`'s keep_types skip.
        keep = set((await effective_vocabulary(self._vocab, self._settings)).entity_like_types)
        content_paths = [p for p in record.node_paths if p.split("/", 1)[0] not in keep]
        if content_paths:
            await asyncio.to_thread(self._writer.remove_nodes, content_paths)
            # DB-delete is decoupled from the unlink result + unconditional: a retry after a crash
            # between the unlink and here (files already gone) still prunes the orphaned index rows
            # (delete is a no-op on absent paths), so no removed node lingers in search/chat.
            await self._index.delete_nodes(content_paths)
        await self._store.tombstone(capture_id)
        await self._backup.request_commit(f"remove chat memory {capture_id}")
        logger.info(
            "removed auto-recorded chat memory %s (%d node file(s))", capture_id, len(content_paths)
        )


def _snippet(text: str, limit: int) -> str:
    """A whitespace-collapsed preview of the endorsed statement, bounded to ``limit`` chars."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


class PgAutoRecordedStore:
    """asyncpg-backed audit registry + remove tombstone — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db, *, snippet_max: int = 200) -> None:
        self._db = db
        # Bound on the audit-list preview (reuses the search snippet length in prod, rule 9).
        self._snippet_max = snippet_max

    async def record(self, capture_id: str, *, salience: str | None) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO chat_auto_recorded (capture_id, salience)
                VALUES ($1, $2)
                ON CONFLICT (capture_id) DO NOTHING
                """,
                capture_id,
                salience,
            )

    async def is_recorded(self, capture_id: str) -> bool:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM chat_auto_recorded WHERE capture_id = $1", capture_id
            )
        return row is not None

    async def tombstone(self, capture_id: str) -> bool:
        async with self._db.acquire() as conn:
            result = await conn.execute(
                "UPDATE captures SET removed_at = now(), updated_at = now() "
                "WHERE id = $1 AND removed_at IS NULL",
                capture_id,
            )
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError):
            return False

    async def list_recent(
        self, limit: int, *, entity_types: list[str]
    ) -> list[AutoRecordedItem]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ar.capture_id, ar.salience, c.node_paths, c.source_ref,
                       c.raw_text, c.created_at, n.title
                  FROM chat_auto_recorded ar
                  JOIN captures c ON c.id = ar.capture_id
             LEFT JOIN LATERAL (
                       SELECT title
                         FROM nodes
                        WHERE store_path = ANY(c.node_paths)
                          AND type <> ALL($2::text[])
                     ORDER BY node_created_at ASC NULLS LAST, store_path ASC
                        LIMIT 1
                       ) n ON true
                 WHERE c.removed_at IS NULL
              ORDER BY c.created_at DESC, ar.capture_id DESC
                 LIMIT $1
                """,
                limit,
                entity_types,
            )
        return [
            AutoRecordedItem(
                capture_id=str(row["capture_id"]),
                node_paths=list(row["node_paths"] or []),
                title=row["title"],
                snippet=_snippet(row["raw_text"] or "", self._snippet_max),
                salience=row["salience"],
                source_ref=row["source_ref"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
