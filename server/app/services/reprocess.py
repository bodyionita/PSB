"""The reusable ``reprocess-all-from-raw`` admin operation (04-pipelines §reprocess, ADR-042).

The standing mechanism for the data-survival principle (vision P10): a fix that changes how nodes
are produced or their on-disk/DB shape heals **already-ingested** data by replaying every capture's
retained raw input through the current pipeline, rather than leaving old derived artifacts broken.

    reset derived state  →  replay every capture's raw (chronological)  →  recompute derived edges
      →  one force commit + push

**Reset contract (ADR-042 §2).** *Always kept:* raw ``captures`` (text/audio/source_ref) + the
graph-store git history (deletions are unlinks). *Preserved (human governance):* approved vocabulary
additions (``app_settings``); standing merges are reported (re-apply is a documented follow-up —
there are zero merges today). *Rebuilt from raw:* every node file, the DB index
(``nodes``/``chunks``/``edges``/``node_profiles``), the alias index, and the ``review_queue``
(entity-ambiguity / vocab-proposal items are capture-derived and re-minted by the replay).

**Safety (ADR-042 §3).** Destructive of derived state → admin-gated + **confirm-required** (the
router's two-step). Single-flight; runs in the background with an ``agent_runs`` row + a
human-readable summary (rule 7). Idempotent (rule 6): the replay is chronological so alias accretion
(ADR-040) rebuilds deterministically. Raw is never touched, so a bad reprocess is recovered by
fixing code and reprocessing again.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from ..config import Settings
from ..db import Database
from ..graph.node_writer import NodeWriter
from .agent_runs import FAILED, SUCCEEDED, AgentRunStore
from .capture_pipeline import ReprocessOne
from .store_backup import StoreCommitter

logger = logging.getLogger(__name__)

# agent_runs.agent name for this op (visible in the activity feed, vision P8).
AGENT = "reprocess-all"


class CaptureReprocessor(Protocol):
    """What the op needs from the capture pipeline: re-ingest one capture from its stored raw."""

    async def reprocess_capture(self, capture_id: str) -> ReprocessOne: ...


class GraphRecomputer(Protocol):
    """The derived-edge recompute surface (:class:`~app.graph.service.DerivedEdgeGraph`)."""

    async def recompute(self) -> object: ...


class ReprocessStore(Protocol):
    """The DB reset + capture-order reads the op relies on (plain SQL, ADR-011)."""

    async def counts(self) -> tuple[int, int]:
        """``(captures, nodes)`` for the confirm-preview."""
        ...

    async def count_merges(self) -> int:
        """Standing entity merges (tombstones) — reported, not silently dropped (ADR-042 §4)."""
        ...

    async def reset_derived_and_review(self) -> None:
        """Truncate the derived index (``nodes`` cascades ``chunks``/``edges``/``node_profiles``) +
        the ``review_queue``, and clear ``captures.node_paths``. Raw + vocab (``app_settings``) are
        untouched."""
        ...

    async def capture_ids_chronological(self) -> list[str]:
        """Every capture id, oldest first — the replay order (ADR-042 §1)."""
        ...


@dataclass(frozen=True)
class ReprocessPreview:
    """The confirm-step preview: what a reprocess would touch (no writes)."""

    captures: int
    nodes: int
    merges: int


class ReprocessService:
    """Owns the reprocess-all pass + a single-flight guard (a reprocess never overlaps itself)."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: ReprocessStore,
        reprocessor: CaptureReprocessor,
        node_writer: NodeWriter,
        store_backup: StoreCommitter,
        run_store: AgentRunStore,
        graph: GraphRecomputer | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._reprocessor = reprocessor
        self._writer = node_writer
        self._backup = store_backup
        self._runs = run_store
        self._graph = graph
        self._ignore = set(settings.store_ignore)
        self._running = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def running(self) -> bool:
        return self._running

    async def preview(self) -> ReprocessPreview:
        """The confirm-step preview (no writes): captures to replay + current derived counts."""
        captures, nodes = await self._store.counts()
        merges = await self._store.count_merges()
        return ReprocessPreview(captures=captures, nodes=nodes, merges=merges)

    async def apply(self) -> str | None:
        """Confirm-step: claim the single-flight slot, open the run, kick off the pass in the
        background, and return its ``run_id``. ``None`` when one is already running (→409)."""
        if self._running:
            return None
        self._running = True
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:
            self._running = False
            raise
        self._spawn(self._run_and_release(run_id))
        return run_id

    async def drain(self) -> None:
        """Await any in-flight reprocess (shutdown / tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- pass core ---------------------------------------------------------------------------

    async def _run_and_release(self, run_id: str) -> None:
        try:
            await self._execute(run_id)
        finally:
            self._running = False

    async def _execute(self, run_id: str) -> None:
        """reset → replay chronologically → recompute derived edges → force commit. A failure ends
        the run ``failed`` with context (rule 7); raw is truth, so a re-run recovers it."""
        try:
            # Report (never silently drop) any standing merges the rebuild can't re-apply by id.
            merges = await self._store.count_merges()
            # 1. Reset derived state (store files + DB index + review queue). Vocab + raw kept.
            removed = await asyncio.to_thread(self._writer.remove_all_nodes, ignore=self._ignore)
            await self._store.reset_derived_and_review()

            # 2. Replay every capture's raw through the current pipeline, oldest first, so entity
            #    accretion rebuilds deterministically (ADR-042 §1). Sequential (awaited).
            ids = await self._store.capture_ids_chronological()
            ok = failed = nodes = inbox = 0
            for capture_id in ids:
                outcome = await self._reprocessor.reprocess_capture(capture_id)
                if outcome.ok:
                    ok += 1
                    nodes += outcome.node_count
                    if outcome.used_inbox_fallback:
                        inbox += 1
                else:
                    failed += 1

            # 3. Recompute derived `similar` edges over the rebuilt vectors (search parity), then
            #    one force commit + push under the store lock (ADR-014).
            if self._graph is not None:
                await self._graph.recompute()
            backup = await self._backup.backup_now("reprocess-all-from-raw")

            summary = (
                f"reprocess-all: {ok}/{len(ids)} captures re-ingested ({nodes} node(s), "
                f"{inbox} inbox), {failed} failed; removed {removed} file(s); push={backup.pushed}"
            )
            if merges:
                summary += f"; ⚠ {merges} standing merge(s) NOT re-applied (re-merge manually)"
                logger.warning(
                    "reprocess-all: %d standing merge(s) could not be re-applied by id "
                    "(re-identify-and-re-apply is a documented follow-up, ADR-042 §4)", merges
                )
            logger.info("%s", summary)
            await self._runs.finish(
                run_id,
                status=SUCCEEDED,
                summary=summary,
                details={
                    "captures": len(ids),
                    "reingested": ok,
                    "failed": failed,
                    "inbox_fallback": inbox,
                    "nodes": nodes,
                    "removed_files": removed,
                    "standing_merges_not_reapplied": merges,
                    "pushed": backup.pushed,
                },
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("reprocess-all failed")
            await self._safe_finish(run_id, exc)

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="reprocess-all failed",
                details={},
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close reprocess-all agent_runs row %s", run_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class PgReprocessStore:
    """asyncpg-backed reset + capture-order reads — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def counts(self) -> tuple[int, int]:
        async with self._db.acquire() as conn:
            captures = await conn.fetchval("SELECT count(*) FROM captures")
            nodes = await conn.fetchval("SELECT count(*) FROM nodes")
        return int(captures or 0), int(nodes or 0)

    async def count_merges(self) -> int:
        async with self._db.acquire() as conn:
            value = await conn.fetchval("SELECT count(*) FROM nodes WHERE merged_into IS NOT NULL")
        return int(value or 0)

    async def reset_derived_and_review(self) -> None:
        async with self._db.transaction() as conn:
            # TRUNCATE nodes CASCADE clears chunks/edges/node_profiles (FK→nodes cascade); captures
            # has no FK to nodes, so it (and agent_runs / app_settings / chat_*) is untouched.
            await conn.execute("TRUNCATE nodes CASCADE")
            await conn.execute("TRUNCATE review_queue")
            await conn.execute("UPDATE captures SET node_paths = '{}'")

    async def capture_ids_chronological(self) -> list[str]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM captures ORDER BY created_at ASC, id ASC")
        return [str(r["id"]) for r in rows]


def build_reprocess_service(
    settings: Settings, db: Database, store_backup: StoreCommitter
) -> ReprocessService:
    """Construct a standalone reprocess service (full pipeline + reset store) for the CLI
    entrypoint (``python -m app.cli reprocess-all``). Mirrors the ``main.py`` wiring but assembles
    only what the op needs, so a fresh process can drive the pass without the HTTP app."""
    # Imported here (not at module top) so the CLI's minimal context builds these lazily.
    from ..entities.resolver import EntityResolver
    from ..entities.store import PgAliasStore
    from ..graph.service import DerivedEdgeGraph
    from ..graph.store import PgGraphStore
    from ..indexing.indexer import Indexer
    from ..indexing.store import PgIndexStore
    from ..providers.registry import build_registry
    from ..tags.store import PgTagStore
    from ..vocab.consolidation import VocabConsolidation
    from ..vocab.service import VocabularyService
    from ..vocab.store import PgVocabularyStore
    from .agent_runs import PgAgentRunStore
    from .capture_pipeline import CapturePipeline
    from .capture_store import PgCaptureStore
    from .review_queue import PgReviewQueue

    registry = build_registry(settings)
    run_store = PgAgentRunStore(db)
    index_store = PgIndexStore(db)
    indexer = Indexer(settings=settings, store=index_store, registry=registry)
    node_writer = NodeWriter(settings.graph_store_path)
    review_queue = PgReviewQueue(db)
    vocabulary_service = VocabularyService(
        settings=settings,
        vocab_store=PgVocabularyStore(db),
        review_store=review_queue,
        consolidation=VocabConsolidation(run_store=run_store),
    )
    entity_resolver = EntityResolver(
        settings=settings,
        alias_store=PgAliasStore(db),
        review_queue=review_queue,
        registry=registry,
        vocab=vocabulary_service,
    )
    pipeline = CapturePipeline(
        settings=settings,
        store=PgCaptureStore(db),
        registry=registry,
        node_writer=node_writer,
        store_backup=store_backup,
        run_store=run_store,
        indexer=indexer,
        entity_resolver=entity_resolver,
        review_queue=review_queue,
        tag_vocabulary=PgTagStore(db),
        vocab=vocabulary_service,
    )
    graph = DerivedEdgeGraph(settings=settings, store=PgGraphStore(db))
    return ReprocessService(
        settings=settings,
        store=PgReprocessStore(db),
        reprocessor=pipeline,
        node_writer=node_writer,
        store_backup=store_backup,
        run_store=run_store,
        graph=graph,
    )
