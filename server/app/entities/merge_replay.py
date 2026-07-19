"""Reprocess replay of durable entity merges (ADR-064 §1) — the step that makes a merge *stick*.

``reprocess-all`` rebuilds every entity from raw with fresh ids, so a manual merge keyed on the
loser's old node id can't be re-applied by id (ADR-042 §4). This service closes that gap: after the
raw replay has re-created the nodes + edges, it reads every durable :class:`MergeDecision`
(:mod:`app.entities.merge_store`) and, for each, resolves both sides to the freshly-created hubs
**by surface form + type** and re-folds the loser into the survivor (shared :func:`fold_entities`).
Merges thus become idempotent across any number of rebuilds — the Diana case (+ future dupes) stays
merged.

Conservative + never-lose (rule 2): a decision whose survivor or loser no longer resolves (renamed
away, deleted, or ambiguous → both sides resolve to the same node) is **skipped**, not guessed; a
per-decision fold failure is logged + skipped, never aborting the reprocess. The service depends on
protocols, so it unit-tests against fakes (no live DB/LLM — 08 testing policy).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..graph.node_writer import NodeWriter
from .entity_store import EntityStore
from .merge import fold_entities
from .merge_core import MergeCore
from .merge_store import MergeDecisionStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayOutcome:
    """One replay pass — how many decisions existed, were re-applied, and were skipped (unresolvable
    or failed). Feeds the reprocess run's summary + details (ADR-064 §1)."""

    decisions: int = 0
    applied: int = 0
    skipped: int = 0


class MergeReplayService:
    """Re-applies the durable merge decisions after a reprocess raw rebuild (ADR-064 §1)."""

    def __init__(
        self,
        *,
        decision_store: MergeDecisionStore,
        entity_store: EntityStore,
        merge_core: MergeCore,
        node_writer: NodeWriter,
    ) -> None:
        self._decisions = decision_store
        self._entities = entity_store
        self._core = merge_core
        self._writer = node_writer

    async def replay(self) -> ReplayOutcome:
        """Re-fold every durable decision's loser into its survivor (matched by surface form/type).

        Called by the reprocess pass **after** the capture replay (so both hubs + their inbound
        edges exist) and **before** the derived recompute (so similarity/profiles reflect the merged
        graph). Never raises — an unresolvable or failing decision is skipped (rule 7)."""
        decisions = await self._decisions.all_decisions()
        applied = 0
        skipped = 0
        for d in decisions:
            survivor_id = await self._entities.find_entity_by_surface_forms(
                d.survivor_forms, node_type=d.survivor_type
            )
            loser_id = await self._entities.find_entity_by_surface_forms(
                d.loser_forms, node_type=d.loser_type
            )
            # Both sides must resolve to distinct live hubs — else the merge can't be re-applied
            # safely (a side gone/renamed, or ambiguous where both resolve to the same node).
            if not survivor_id or not loser_id or survivor_id == loser_id:
                skipped += 1
                continue
            survivor = await self._entities.get_node(survivor_id)
            loser = await self._entities.get_node(loser_id)
            if (
                survivor is None
                or loser is None
                or survivor.merged_into is not None
                or loser.merged_into is not None
            ):
                skipped += 1
                continue
            try:
                await fold_entities(
                    loser=loser,
                    survivor=survivor,
                    node_writer=self._writer,
                    merge_core=self._core,
                    reason=f"reprocess replay merge {loser.id} → {survivor.id}",
                )
                applied += 1
            except Exception:  # noqa: BLE001 — one bad decision never aborts the reprocess (rule 7)
                logger.exception(
                    "merge replay: fold %s → %s failed (skipped)", loser_id, survivor_id
                )
                skipped += 1
        logger.info(
            "merge replay: %d/%d durable merge(s) re-applied (%d skipped)",
            applied,
            len(decisions),
            skipped,
        )
        return ReplayOutcome(decisions=len(decisions), applied=applied, skipped=skipped)


def build_merge_replay(settings, db, store_backup) -> MergeReplayService:
    """Assemble a standalone merge-replay service for the reprocess CLI entrypoint
    (``python -m app.cli reprocess-all``) — mirrors the ``main.py`` wiring but builds only what the
    replay needs: the durable-decision store, the entity read store, and a merge-core (its own
    indexer + node writer + node-media store) so a fresh process re-folds without the HTTP app."""
    from ..indexing.indexer import Indexer
    from ..indexing.store import PgIndexStore
    from ..providers.registry import build_registry
    from ..services.node_media_store import PgNodeMediaStore
    from .entity_store import PgEntityStore
    from .merge_store import PgMergeDecisionStore

    node_writer = NodeWriter(settings.graph_store_path)
    entity_store = PgEntityStore(db)
    indexer = Indexer(settings=settings, store=PgIndexStore(db), registry=build_registry(settings))
    merge_core = MergeCore(
        entity_store=entity_store,
        node_writer=node_writer,
        indexer=indexer,
        store_backup=store_backup,
        node_media=PgNodeMediaStore(db),
    )
    return MergeReplayService(
        decision_store=PgMergeDecisionStore(db),
        entity_store=entity_store,
        merge_core=merge_core,
        node_writer=node_writer,
    )
