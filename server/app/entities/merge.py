"""Entity merge — ``POST /admin/entities/merge`` propose→apply + tombstones (ADR-030 §5, task 6).

``merge(loser → survivor)`` folds one entity onto another. Like tag consolidation (ADR-024 §2) it
is a deliberate **two-step**, so a human reviews the blast radius before any write:

  * :meth:`propose` — synchronous, **no writes, no LLM**: validate the pair and return the
    inbound-edge inventory (the reverse index — every node that points at the loser) plus both
    entities' alias sets, so the UI can confirm.
  * :meth:`apply` — opens an ``agent="entity-merge"`` run and, in the **background**, rewrites the
    store then reindexes + force-commits, answering ``202 {run_id}`` (03-api §Admin). Concretely
    (ADR-030 §5): every inbound edge is **retargeted** loser→survivor on the source files; the
    survivor's ``aliases`` are **unioned** with the loser's name + aliases; the loser file is
    replaced by a permanent **tombstone** (``merged_into: <survivor>`` — old ids/source_refs keep
    resolving, the node is hidden from search/map); the touched files are reindexed; and a forced
    commit+push checkpoints it in git history immediately (no agent-window wait — ADR-014 is the
    safety net, so a wrong merge is a ``git revert``).

The store is truth (rule 1): the service rewrites **files** and lets the indexer re-materialize the
edges/aliases into the DB — it never writes the graph tables directly. Every write is atomic +
git-revertible, and per-node failures are skip-and-continue (rule 7), so a partial apply is safe to
re-drive. The service depends on protocols, so it unit-tests against fakes (08 testing policy).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from ..config import Settings
from ..graph.node_writer import NodeWriter, merged_alias_union
from ..indexing.indexer import NodeIndexer
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.store_backup import StoreCommitter
from .entity_store import EntityNode, EntityStore, InboundEdge

logger = logging.getLogger(__name__)

# agent_runs.agent name for the merge apply (visible in the activity feed, vision P8).
AGENT = "entity-merge"


class MergeError(Exception):
    """Base for merge problems surfaced to the API layer."""


class MergeNodeNotFound(MergeError):
    """The loser or survivor id is unknown (404)."""


class BadMerge(MergeError):
    """The merge is invalid — same node, a non-entity type, or a tombstone endpoint (400)."""


@dataclass(frozen=True)
class InboundEntry:
    """One inbound edge in the propose inventory (a source node that points at the loser)."""

    src_id: str
    src_store_path: str
    rel: str


@dataclass(frozen=True)
class MergeSide:
    """A loser/survivor summary for the propose response (identity + alias set)."""

    id: str
    type: str
    title: str | None
    aliases: list[str]


@dataclass(frozen=True)
class MergeProposal:
    """The propose result: a correlation id + both sides + the inbound inventory (no writes)."""

    plan_id: str
    loser: MergeSide
    survivor: MergeSide
    inbound: list[InboundEntry] = field(default_factory=list)

    @property
    def inbound_count(self) -> int:
        return len(self.inbound)


class MergeService:
    """Owns entity-merge propose (inventory) + apply (rewrite store → reindex → commit)."""

    def __init__(
        self,
        *,
        settings: Settings,
        entity_store: EntityStore,
        node_writer: NodeWriter,
        indexer: NodeIndexer,
        store_backup: StoreCommitter,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._entities = entity_store
        self._writer = node_writer
        self._indexer = indexer
        self._backup = store_backup
        self._runs = run_store
        self._tasks: set[asyncio.Task] = set()

    # --- propose ----------------------------------------------------------------------------

    async def propose(self, loser_id: str, survivor_id: str) -> MergeProposal:
        """Validate the pair + return the inbound-edge inventory (no writes). Raises
        :class:`BadMerge`/:class:`MergeNodeNotFound` for the router to map (400/404)."""
        loser, survivor = await self._validated_pair(loser_id, survivor_id)
        inbound = await self._entities.inbound_canonical_edges(loser.id)
        return MergeProposal(
            plan_id=uuid.uuid4().hex,
            loser=_side(loser),
            survivor=_side(survivor),
            inbound=[
                InboundEntry(src_id=e.src_id, src_store_path=e.src_store_path, rel=e.rel)
                for e in inbound
            ],
        )

    # --- apply ------------------------------------------------------------------------------

    async def apply(self, loser_id: str, survivor_id: str) -> str:
        """Re-validate, open the run, and rewrite+reindex+commit in the background. Returns run_id.

        Validation runs synchronously so a bad request still gets a 400/404 (not a failed run);
        the store mutation runs in the background so the endpoint answers ``202 {run_id}``."""
        loser, survivor = await self._validated_pair(loser_id, survivor_id)
        run_id = await self._runs.start(AGENT)
        self._spawn(self._run_apply(run_id, loser, survivor))
        return run_id

    async def _run_apply(self, run_id: str, loser: EntityNode, survivor: EntityNode) -> None:
        """Retarget inbound edges → union aliases → tombstone the loser → reindex → commit+push.

        Never raises (rule 7): a per-file read/write error is logged + skipped; any unexpected
        failure ends the run ``failed`` with context. Files are truth + git-revertible, so a partial
        apply is safe to re-drive."""
        try:
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
                    logger.warning("merge: source %s gone; edge not retargeted (skipped)", path)
                    skipped += 1
                    continue
                if count:
                    retargeted += count
                    changed.append(path)

            # Union the survivor's aliases with the loser's surface forms, then tombstone the loser.
            new_aliases = merged_alias_union(
                survivor.aliases, survivor.title, loser.aliases, loser.title
            )
            try:
                await asyncio.to_thread(self._writer.set_aliases, survivor.store_path, new_aliases)
                if survivor.store_path not in changed:
                    changed.append(survivor.store_path)
            except FileNotFoundError:
                logger.warning(
                    "merge: survivor file %s gone; aliases not unioned", survivor.store_path
                )

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
            backup = await self._backup.backup_now(
                f"merge {loser.id} → {survivor.id}"
            )

            summary = (
                f"entity merge: {loser.title or loser.id} → {survivor.title or survivor.id} "
                f"({retargeted} edge(s) retargeted across {len(changed)} file(s), "
                f"{len(new_aliases)} alias(es) on survivor)"
            )
            if skipped:
                summary += f", {skipped} source(s) skipped"
            logger.info("%s (pushed=%s)", summary, backup.pushed)
            await self._runs.finish(
                run_id,
                status=SUCCEEDED,
                summary=summary,
                details={
                    "loser": loser.id,
                    "survivor": survivor.id,
                    "edges_retargeted": retargeted,
                    "files_changed": len(changed),
                    "sources_skipped": skipped,
                    "survivor_aliases": new_aliases,
                    "index": index.as_dict() if index is not None else None,
                    "commit": {"committed": backup.committed, "pushed": backup.pushed},
                },
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("entity merge apply failed")
            await self._safe_finish(run_id, exc)

    # --- helpers ----------------------------------------------------------------------------

    async def _validated_pair(
        self, loser_id: str, survivor_id: str
    ) -> tuple[EntityNode, EntityNode]:
        if loser_id == survivor_id:
            raise BadMerge("cannot merge a node into itself")
        loser = await self._entities.get_node(loser_id)
        survivor = await self._entities.get_node(survivor_id)
        if loser is None or survivor is None:
            missing = loser_id if loser is None else survivor_id
            raise MergeNodeNotFound(missing)
        if loser.merged_into is not None:
            raise BadMerge(f"loser {loser_id} is already a tombstone (merged away)")
        if survivor.merged_into is not None:
            raise BadMerge(f"survivor {survivor_id} is a tombstone; merge into its survivor")
        entity_types = set(self._settings.entity_like_types)
        if loser.type not in entity_types or survivor.type not in entity_types:
            raise BadMerge("merge is for entity-like nodes only (aliases substrate — ADR-030)")
        return loser, survivor

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="entity merge failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close entity-merge agent_runs row %s", run_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Await any in-flight background apply (used on shutdown / in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def _side(node: EntityNode) -> MergeSide:
    return MergeSide(id=node.id, type=node.type, title=node.title, aliases=list(node.aliases))


def _distinct_source_paths(inbound: list[InboundEdge]) -> list[str]:
    """Distinct source store paths (a node with several edges to the loser is rewritten once)."""
    seen: list[str] = []
    for edge in inbound:
        if edge.src_store_path not in seen:
            seen.append(edge.src_store_path)
    return seen
