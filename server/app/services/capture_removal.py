"""Shared capture-remove core (ADR-048 §11 / ADR-062 §R).

Two surfaces remove a capture, and they share one hub-preserving node-removal primitive:

* **Chat one-tap remove** (``POST /chat/auto-recorded/{id}/remove``, :mod:`app.chat.auto_recorded`)
  — soft-delete an *auto-endorsed* chat memory: git-rm its content nodes + tombstone the capture.
  It gates on the audit registry (auto-endorsed only) and keeps its media (chat memories have none).

* **General capture remove** (``DELETE /captures/{id}`` — :class:`CaptureRemovalService`) — the
  ADR-062 §R generalization: **entirely delete** any submitted capture — content nodes (entity hubs
  preserved, ADR-038), **plus** its ``media`` rows + raw files ("entirely delete", the
  user-initiated carve-out to rule 2, same as draft discard but post-submit) — then tombstone the
  capture row.

The mechanical core both share — compute the capture's **content** paths (``node_paths`` minus the
entity-hub folders), git-rm them, prune their index rows — lives in :func:`remove_content_nodes`, so
hub preservation + the self-healing delete order are identical on both paths. The general remove
adds the media purge, the open-draft guard (409 — that's Discard's job), and the capture tombstone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from ..config import Settings
from ..graph.node_writer import NodeWriter
from ..vocab.service import VocabularyProvider, effective_vocabulary
from .capture_store import DRAFT, CaptureRecord
from .media_store import MediaFiles, MediaStore
from .store_backup import StoreBackup

logger = logging.getLogger(__name__)


class NodeDeleteStore(Protocol):
    """The index delete the remove op needs — DB rows for the removed node paths
    (``chunks``/``edges``/``node_media`` cascade off ``nodes``, ADR-026/060)."""

    async def delete_nodes(self, store_paths: list[str]) -> int: ...


class CaptureRemoveStore(Protocol):
    """The two capture reads/writes the general remove needs: fetch the record + tombstone it."""

    async def get(self, capture_id: str) -> CaptureRecord | None: ...

    async def tombstone(self, capture_id: str) -> bool: ...


class CaptureRemoveNotFound(Exception):
    """Remove was requested for a capture that is unknown or already removed → ``404``."""


class CaptureRemoveDraftOpen(Exception):
    """Remove was requested for an open ``draft`` capture → ``409`` (Discard's job, not remove)."""


async def remove_content_nodes(
    record: CaptureRecord,
    *,
    entity_like_types: list[str],
    node_writer: NodeWriter,
    index_store: NodeDeleteStore,
) -> list[str]:
    """Git-rm a capture's **content** nodes + prune their index rows; return the removed paths.

    The shared core of both remove paths (ADR-048 §11 / ADR-062 §R). Entity **hubs** are preserved
    (ADR-038): a hub this capture minted is substrate owned by the entity lifecycle, not this
    memory — deleting it would dangle every other node's edge into it. Content = ``node_paths``
    whose folder (``<type>/…``) is not an entity-like type, mirroring the writer's keep-skip.

    Idempotent + self-healing on a retry: the file unlink tolerates already-gone files, and the DB
    delete is keyed to the capture's content paths (not the unlink result) + runs unconditionally,
    so a crash between the unlink and here still prunes the orphaned index rows (a no-op on absent
    rows) — no removed node lingers in search/chat.
    """
    keep = set(entity_like_types)
    content_paths = [p for p in record.node_paths if p.split("/", 1)[0] not in keep]
    if content_paths:
        await asyncio.to_thread(node_writer.remove_nodes, content_paths)
        await index_store.delete_nodes(content_paths)
    return content_paths


class CaptureRemovalService:
    """Owns the **general** capture remove (``DELETE /captures/{id}``, ADR-062 §R)."""

    def __init__(
        self,
        *,
        settings: Settings,
        captures: CaptureRemoveStore,
        index_store: NodeDeleteStore,
        node_writer: NodeWriter,
        store_backup: StoreBackup,
        media_store: MediaStore | None = None,
        media_files: MediaFiles | None = None,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._captures = captures
        self._index = index_store
        self._writer = node_writer
        self._backup = store_backup
        self._media_store = media_store
        self._media_files = media_files
        # Effective entity-like types (seeds ∪ approved additions) — the hub folders remove must
        # preserve (ADR-038). None ⇒ seed-only fallback (tests without governed vocab).
        self._vocab = vocab

    async def remove_capture(self, capture_id: str) -> None:
        """Entirely delete a submitted capture (ADR-062 §R): content nodes (hubs preserved), the
        capture's ``media`` rows + raw files, then tombstone the capture (replay-excluded).

        Raises :class:`CaptureRemoveNotFound` (→404) for an unknown or already-removed capture, and
        :class:`CaptureRemoveDraftOpen` (→409) for an open draft (Discard removes those). Idempotent
        + self-healing: content-node removal and media purge are no-ops on a retry, and the
        tombstone is stamped **last**, so a failure before it leaves the capture live
        (``removed_at`` NULL) and a re-run redoes the (already-gone) deletes first — never
        half-removed.
        """
        record = await self._captures.get(capture_id)
        if record is None or record.removed_at is not None:
            raise CaptureRemoveNotFound(capture_id)
        if record.status == DRAFT:
            raise CaptureRemoveDraftOpen(capture_id)

        entity_types = list(
            (await effective_vocabulary(self._vocab, self._settings)).entity_like_types
        )
        content_paths = await remove_content_nodes(
            record,
            entity_like_types=entity_types,
            node_writer=self._writer,
            index_store=self._index,
        )
        media_count = await self._purge_media(capture_id)
        # Tombstone LAST — the self-heal boundary (see docstring). A capture whose row already
        # tombstoned between the get and here (double-tap) is a harmless no-op (scoped UPDATE).
        await self._captures.tombstone(capture_id)
        await self._backup.request_commit(f"remove capture {capture_id}")
        logger.info(
            "removed capture %s (%d content node(s) + %d media purged)",
            capture_id,
            len(content_paths),
            media_count,
        )

    async def _purge_media(self, capture_id: str) -> int:
        """Delete the capture's ``media`` rows + their raw files ("entirely delete", ADR-062 §R).

        Raw file first, then the row (mirrors ``_delete_capture_with_media`` ordering): a mid-purge
        crash can only orphan a row whose file is already gone, which the tombstone-driven retry
        re-purges. No-op when the media substrate is unwired (a text/chat pipeline)."""
        if self._media_store is None or self._media_files is None:
            return 0
        media = await self._media_store.list_by_capture_id(capture_id)
        for m in media:
            if m.file_path:
                await self._media_files.delete_async(m.file_path)
            await self._media_store.delete(m.id)
        return len(media)
