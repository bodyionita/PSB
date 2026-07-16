"""The shared merge-core (ADR-049 §1) — the mechanism both entity-merge and content-merge fold on.

Merging one node onto another is the same store rewrite regardless of *what* the nodes are: every
inbound canonical edge is **retargeted** loser→survivor across its source files, the loser file is
replaced by a permanent **tombstone** (``merged_into: <survivor>`` — old ids/source_refs keep
resolving, the node is hidden from search/map), the touched files are **reindexed**, and a forced
commit+push checkpoints it in git history immediately (ADR-014 is the safety net — a wrong merge is
a ``git revert``). That mechanism is :class:`MergeCore`; the two callers add only what differs:

  * **Entity-merge** (ADR-030 §5) composes the core with an **alias-union** on top — it writes the
    survivor's unioned aliases first, then calls ``fold(..., survivor_extra_paths=[survivor.path])``
    so the rewritten survivor file is reindexed in the same pass.
  * **Content-merge** (dedup, ADR-049 §1) is ``fold(...)`` **alone** — content nodes are not the
    alias substrate, so there is no union; the survivor keeps its own type, the loser is tombstoned.

The store is truth (rule 1): the core rewrites *files* and lets the indexer re-materialize the
``edges``/tombstone into the DB — it never writes the graph tables directly. Per-file failures are
skip-and-continue (rule 7); a partial fold is git-revertible and safe to re-drive. Run management
(``agent_runs``) is the caller's concern — the core is a pure store operation with no run/LLM.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..graph.node_writer import NodeWriter
from ..indexing.indexer import NodeIndexer
from ..services.store_backup import StoreCommitter
from .entity_store import EntityStore, InboundEdge

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeTarget:
    """The minimum a fold needs about either side: identity + type + where its file lives. Both the
    ``EntityNode`` (entity-merge) and a ``get_node`` read (content-merge) project onto this."""

    id: str
    type: str
    title: str | None
    store_path: str


@dataclass(frozen=True)
class MergeCoreResult:
    """The outcome of one fold — the retarget/skip counts + the files touched + the commit result,
    so a caller composes its own summary + ``agent_runs`` details from it (ADR-049 §1)."""

    edges_retargeted: int = 0
    changed_paths: list[str] = field(default_factory=list)
    sources_skipped: int = 0
    committed: bool = False
    pushed: bool = False
    index: dict[str, object] | None = None

    @property
    def files_changed(self) -> int:
        return len(self.changed_paths)


class MergeCore:
    """Retarget inbound edges → tombstone the loser → reindex → force commit+push (ADR-049 §1)."""

    def __init__(
        self,
        *,
        entity_store: EntityStore,
        node_writer: NodeWriter,
        indexer: NodeIndexer,
        store_backup: StoreCommitter,
    ) -> None:
        self._entities = entity_store
        self._writer = node_writer
        self._indexer = indexer
        self._backup = store_backup

    async def fold(
        self,
        *,
        loser: MergeTarget,
        survivor: MergeTarget,
        reason: str,
        survivor_extra_paths: Sequence[str] = (),
    ) -> MergeCoreResult:
        """Fold ``loser`` into ``survivor`` and checkpoint it. ``survivor_extra_paths`` are files
        the caller has already rewritten for this merge (e.g. the alias-unioned survivor) that must
        be reindexed in the same pass. Never raises for a per-file read/write miss (rule 7) — a
        vanished source is logged + skipped; the loser is still tombstoned and the pass commits."""
        inbound = await self._entities.inbound_canonical_edges(loser.id)
        changed: list[str] = []
        retargeted = 0
        skipped = 0
        for path in _distinct_source_paths(inbound):
            try:
                count = await asyncio.to_thread(
                    self._writer.retarget_edges, path, old_to=loser.id, new_to=survivor.id
                )
            except FileNotFoundError:
                logger.warning("merge-core: source %s gone; edge not retargeted (skipped)", path)
                skipped += 1
                continue
            if count:
                retargeted += count
                changed.append(path)

        # Files the caller rewrote for this merge (the alias-unioned survivor) are reindexed too.
        for path in survivor_extra_paths:
            if path not in changed:
                changed.append(path)

        # Replace the loser with a tombstone so old ids/source_refs redirect to the survivor.
        await asyncio.to_thread(
            self._writer.write_tombstone,
            loser.store_path,
            node_id=loser.id,
            node_type=loser.type,
            survivor_id=survivor.id,
        )
        if loser.store_path not in changed:
            changed.append(loser.store_path)

        index = await self._indexer.index_paths(changed) if changed else None
        backup = await self._backup.backup_now(reason)
        logger.info(
            "merge-core: %s → %s (%d edge(s) retargeted, %d file(s), %d skipped, pushed=%s)",
            loser.id,
            survivor.id,
            retargeted,
            len(changed),
            skipped,
            backup.pushed,
        )
        return MergeCoreResult(
            edges_retargeted=retargeted,
            changed_paths=changed,
            sources_skipped=skipped,
            committed=backup.committed,
            pushed=backup.pushed,
            index=index.as_dict() if index is not None else None,
        )


def _distinct_source_paths(inbound: list[InboundEdge]) -> list[str]:
    """Distinct source store paths (a node with several edges to the loser is rewritten once)."""
    seen: list[str] = []
    for edge in inbound:
        if edge.src_store_path not in seen:
            seen.append(edge.src_store_path)
    return seen
