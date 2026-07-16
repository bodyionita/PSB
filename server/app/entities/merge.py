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
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..vocab.service import VocabularyProvider, effective_vocabulary
from .entity_store import EntityNode, EntityStore
from .merge_core import MergeCore, MergeTarget

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
    """Owns entity-merge propose (inventory) + apply (alias-union + the shared fold, ADR-049 §1)."""

    def __init__(
        self,
        *,
        settings: Settings,
        entity_store: EntityStore,
        node_writer: NodeWriter,
        merge_core: MergeCore,
        run_store: AgentRunStore,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._entities = entity_store
        # The alias union writes the survivor file directly (the entity-only half); the retarget →
        # tombstone → reindex → commit half is the shared MergeCore (ADR-049 §1).
        self._writer = node_writer
        self._core = merge_core
        self._runs = run_store
        # Effective entity-like types (seeds ∪ approved additions — ADR-027/035); None ⇒ seeds.
        self._vocab = vocab
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
        """Union aliases onto the survivor, then fold the loser into it via the shared MergeCore
        (retarget inbound edges → tombstone loser → reindex → commit+push, ADR-049 §1).

        The alias write simply moves ahead of the retarget vs the pre-extraction service — both are
        disjoint frontmatter regions, so the end state is identical (ADR-049 §1). Never raises (rule
        7): the core skips a per-file miss; any unexpected failure ends the run ``failed`` with
        context. Files are truth + git-revertible, so a partial apply is safe to re-drive."""
        try:
            # Union the survivor's aliases with the loser's surface forms (the entity-only half),
            # then reindex that survivor file in the same fold pass via ``survivor_extra_paths``.
            new_aliases = merged_alias_union(
                survivor.aliases, survivor.title, loser.aliases, loser.title
            )
            survivor_paths: list[str] = []
            try:
                await asyncio.to_thread(self._writer.set_aliases, survivor.store_path, new_aliases)
                survivor_paths.append(survivor.store_path)
            except FileNotFoundError:
                logger.warning(
                    "merge: survivor file %s gone; aliases not unioned", survivor.store_path
                )

            result = await self._core.fold(
                loser=_target(loser),
                survivor=_target(survivor),
                reason=f"merge {loser.id} → {survivor.id}",
                survivor_extra_paths=survivor_paths,
            )

            summary = (
                f"entity merge: {loser.title or loser.id} → {survivor.title or survivor.id} "
                f"({result.edges_retargeted} edge(s) retargeted across {result.files_changed} "
                f"file(s), {len(new_aliases)} alias(es) on survivor)"
            )
            if result.sources_skipped:
                summary += f", {result.sources_skipped} source(s) skipped"
            await self._runs.finish(
                run_id,
                status=SUCCEEDED,
                summary=summary,
                details={
                    "loser": loser.id,
                    "survivor": survivor.id,
                    "edges_retargeted": result.edges_retargeted,
                    "files_changed": result.files_changed,
                    "sources_skipped": result.sources_skipped,
                    "survivor_aliases": new_aliases,
                    "index": result.index,
                    "commit": {"committed": result.committed, "pushed": result.pushed},
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
        effective = await effective_vocabulary(self._vocab, self._settings)
        entity_types = set(effective.entity_like_types)
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


def _target(node: EntityNode) -> MergeTarget:
    """Project an ``EntityNode`` onto the core's minimal ``MergeTarget`` (id/type/title/path)."""
    return MergeTarget(id=node.id, type=node.type, title=node.title, store_path=node.store_path)
