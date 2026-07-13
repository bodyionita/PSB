"""Review-queue read/resolve service (03-api §Review, ADR-030 §3 / ADR-029; M3 task 4).

The admin Review surface: list the decidable-in-place items the pipeline filed, and resolve one.
Resolution is where a human decision becomes graph structure — the business logic the router
delegates to (rule 5). Two kinds are resolvable in M3:

  * ``entity-ambiguity`` — the organizer couldn't confidently link an entity mention, so it left
    the edge **pending** + filed candidates (ADR-030 §3). A resolution:
      - ``choice = <candidate id>`` → **materialize** the pending edge (file + DB) onto every
        content node that wanted it, targeting the chosen entity;
      - ``choice = "new"`` → mint a fresh thin entity hub, then materialize the edge onto it;
      - ``choice = "maybe"`` → defer (status ``maybe``), draw nothing.
  * ``vocab-proposal`` — a proposed node/edge type outside the seeded vocabulary (ADR-027):
      - ``verdict = "approve"`` → record the approval + open a **queued** ``vocab-consolidation``
        marker run; the retro-consolidation job that mutates the live vocabulary lands in M3 task 7;
      - ``verdict = "reject"`` → discard.

Materialization reuses the store's own machinery: :meth:`NodeWriter.add_edges` appends the edge to
the node file (atomic, idempotent), then the indexer re-reads that file and materializes the
canonical edge into the ``edges`` table (never bypasses the store — rule 1); a store commit is then
requested (ADR-014). The service depends on protocols so it unit-tests against fakes (no live
DB/LLM — 08 testing policy).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Settings
from ..graph.node_writer import NodeDocument, NodeEdge, NodeWriter
from ..indexing.indexer import NodeIndexer
from ..indexing.store import IndexStore
from .agent_runs import SKIPPED as RUN_SKIPPED
from .agent_runs import AgentRunStore
from .review_queue import (
    KIND_ENTITY_AMBIGUITY,
    KIND_VOCAB_PROPOSAL,
    STATUS_DISCARDED,
    STATUS_MAYBE,
    STATUS_PENDING,
    STATUS_RESOLVED,
    ReviewReadStore,
    ReviewRecord,
)
from .store_backup import StoreBackup

logger = logging.getLogger(__name__)

# The queued marker's agent name — the M3-task-7 consolidation job consumes approvals under it.
AGENT_VOCAB_CONSOLIDATION = "vocab-consolidation"


class ReviewError(Exception):
    """Base for review-resolution problems surfaced to the API layer."""


class ReviewNotFound(ReviewError):
    """No review item with the given id (404)."""


class ReviewNotPending(ReviewError):
    """The item was already resolved/discarded/deferred — it cannot be resolved again (409)."""


class BadResolution(ReviewError):
    """The resolution body is invalid for the item's kind (400)."""


class ReviewService:
    def __init__(
        self,
        *,
        settings: Settings,
        review_store: ReviewReadStore,
        index_store: IndexStore,
        indexer: NodeIndexer,
        node_writer: NodeWriter,
        store_backup: StoreBackup,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._store = review_store
        self._index = index_store
        self._indexer = indexer
        self._writer = node_writer
        self._backup = store_backup
        self._runs = run_store
        self._tz = ZoneInfo(settings.scheduler_tz)

    async def list_items(
        self, *, status: str | None = "pending", kind: str | None = None
    ) -> list[ReviewRecord]:
        """The admin Review list (GET /review). ``status``/``kind`` empty or ``all`` ⇒ no filter."""
        return await self._store.list_items(
            status=_normalize_filter(status),
            kind=_normalize_filter(kind),
            limit=self._settings.review_list_max,
        )

    async def resolve(
        self, review_id: str, *, choice: str | None = None, verdict: str | None = None
    ) -> ReviewRecord:
        """Resolve one review item (POST /review/{id}); returns the updated record.

        Materialization runs before the status transition, so a materialization failure leaves the
        item ``pending`` (retryable) rather than resolved-but-unapplied. Both are idempotent, so the
        (single-user) race where the guarded transition then finds it already resolved is harmless.
        """
        record = await self._store.get(review_id)
        if record is None:
            raise ReviewNotFound(review_id)
        if record.status != STATUS_PENDING:
            raise ReviewNotPending(review_id)

        if record.kind == KIND_ENTITY_AMBIGUITY:
            new_status, resolution = await self._resolve_entity(record, choice)
        elif record.kind == KIND_VOCAB_PROPOSAL:
            new_status, resolution = await self._resolve_vocab(record, verdict)
        else:
            raise BadResolution(f"kind {record.kind!r} is not resolvable in M3")

        await self._store.resolve(review_id, status=new_status, resolution=resolution)
        updated = await self._store.get(review_id)
        return updated if updated is not None else record

    # --- entity-ambiguity ---------------------------------------------------------------

    async def _resolve_entity(
        self, record: ReviewRecord, choice: str | None
    ) -> tuple[str, dict]:
        if not choice:
            raise BadResolution("entity-ambiguity requires a 'choice'")
        if choice == "maybe":
            return STATUS_MAYBE, {"choice": "maybe"}

        pending_edges = _as_pending_edges(record.payload.get("pending_edges"))
        if choice == "new":
            target_id, entity_path = await self._mint_entity(record)
            await self._materialize(target_id, pending_edges, extra_paths=[entity_path])
            return STATUS_RESOLVED, {"choice": "new", "entity_id": target_id}

        candidate_ids = {c.get("id") for c in record.payload.get("candidates", [])}
        if choice not in candidate_ids:
            raise BadResolution("'choice' must be a candidate id, 'new', or 'maybe'")
        await self._materialize(choice, pending_edges)
        return STATUS_RESOLVED, {"choice": choice}

    async def _mint_entity(self, record: ReviewRecord) -> tuple[str, str]:
        """Mint a thin entity hub for the ``new`` choice (title + alias, ADR-030 §4), then index."""
        mention = record.payload.get("mention") or {}
        name = str(mention.get("name") or "").strip()
        entity_type = str(mention.get("type") or "").strip()
        if not name or entity_type not in self._settings.entity_like_types:
            raise BadResolution("cannot mint a new entity: the review item has no usable mention")
        doc = NodeDocument(
            id=str(uuid.uuid4()),
            type=entity_type,
            title=name,
            body="",
            created_local=datetime.now(self._tz),
            source="review",
            source_ref=record.id,
            aliases=(name,),
        )
        written = await asyncio.to_thread(self._writer.write_nodes, [doc])
        return doc.id, written[0].store_path

    async def _materialize(
        self, target_id: str, pending_edges: list[dict], *, extra_paths: list[str] | None = None
    ) -> None:
        """Draw the pending edges onto every source node, then reconcile the DB + commit.

        Each source node's file gets the edge appended (idempotent); re-indexing that file
        materializes the canonical edge into the ``edges`` table from the frontmatter (rule 1 — the
        store is truth, the DB is derived). A source node that is not indexed / has vanished is
        skipped, never fatal (rule 7). ``extra_paths`` (a freshly-minted entity) are indexed too so
        the ``dst_id`` FK is satisfied before the source edges materialize (the indexer upserts all
        nodes before materializing any edges)."""
        paths: list[str] = []
        for edge in pending_edges:
            src_id = edge.get("src")
            rel = edge.get("rel")
            if not src_id or not rel:
                continue
            state = await self._index.get_index_state(src_id)
            if state is None:
                logger.warning(
                    "review: source node %s not indexed; cannot materialize its edge (skipped)",
                    src_id,
                )
                continue
            node_edge = NodeEdge(rel=rel, to=target_id, since=edge.get("since"))
            try:
                await asyncio.to_thread(self._writer.add_edges, state.store_path, [node_edge])
            except FileNotFoundError:
                logger.warning(
                    "review: source node file %s is gone; edge not materialized (skipped)",
                    state.store_path,
                )
                continue
            if state.store_path not in paths:
                paths.append(state.store_path)

        to_index = list(extra_paths or []) + paths
        if to_index:
            await self._indexer.index_paths(to_index)
            await self._backup.request_commit("review: materialize entity edge")

    # --- vocab-proposal -----------------------------------------------------------------

    async def _resolve_vocab(
        self, record: ReviewRecord, verdict: str | None
    ) -> tuple[str, dict]:
        if verdict == "reject":
            return STATUS_DISCARDED, {"verdict": "reject"}
        if verdict != "approve":
            raise BadResolution("vocab-proposal requires a 'verdict' of 'approve' or 'reject'")

        vocab = record.payload.get("vocab")
        value = record.payload.get("value")
        run_id = await self._queue_consolidation(vocab, value, record.id)
        return STATUS_RESOLVED, {
            "verdict": "approve",
            "vocab": vocab,
            "value": value,
            "run_id": run_id,
        }

    async def _queue_consolidation(
        self, vocab: object, value: object, review_id: str
    ) -> str | None:
        """Record the approval as a **queued** ``vocab-consolidation`` marker run (vision P8 —
        everything visible). Task 4 only queues: the retro-consolidation job that mutates the live
        vocabulary and re-walks the graph lands in M3 task 7, which consumes these markers. Opening
        the marker must never fail the resolution (rule 7)."""
        try:
            run_id = await self._runs.start(AGENT_VOCAB_CONSOLIDATION)
            await self._runs.finish(
                run_id,
                status=RUN_SKIPPED,
                summary=(
                    f"vocab approval queued: {vocab} '{value}' — "
                    "retro-consolidation runs in M3 task 7"
                ),
                details={
                    "queued": True,
                    "vocab": vocab,
                    "value": value,
                    "review_id": review_id,
                },
            )
            return run_id
        except Exception:  # noqa: BLE001 — a run-store hiccup must not fail the approval
            logger.exception("could not open the vocab-consolidation marker run (ignored)")
            return None


def _normalize_filter(value: str | None) -> str | None:
    """A query filter: empty / ``all`` (case-insensitive) ⇒ no filter (``None``)."""
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed or trimmed.lower() == "all":
        return None
    return trimmed


def _as_pending_edges(value: object) -> list[dict]:
    """The payload's ``pending_edges`` as a clean list of dicts (tolerant of legacy/absent)."""
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, dict)]
