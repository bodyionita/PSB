"""Review-queue read/resolve service (03-api §Review, ADR-030 §3 / ADR-029; M3 task 4).

The admin Review surface: list the decidable-in-place items the pipeline filed, and resolve one.
Resolution is where a human decision becomes graph structure — the business logic the router
delegates to (rule 5). Three kinds are resolvable:

  * ``entity-ambiguity`` — the organizer couldn't confidently link an entity mention, so it left
    the edge **pending** + filed candidates (ADR-030 §3). A resolution:
      - ``choice = <candidate id>`` → **materialize** the pending edge (file + DB) onto every
        content node that wanted it, targeting the chosen entity;
      - ``choice = "new"`` → mint a fresh thin entity hub, then materialize the edge onto it;
      - ``choice = "maybe"`` → defer (status ``maybe``), draw nothing.
  * ``stance-candidate`` (**M6**, ADR-048 §7) — a chat-distilled memory whose user-stance was
    unclear. A ``verdict``: ``agree`` materializes a ``source=chat`` capture through the pipeline
    (the **exact auto-endorse path** — one ingest path, not two, so P10 holds); ``disagree``
    discards it (logged, never a node); ``maybe`` parks it, **re-openable** (a parked maybe accepts
    a later agree/disagree — the resolve guard treats ``pending`` ∪ ``maybe`` as decidable).
  * ``vocab-proposal`` — a proposed node/edge type outside the seeded vocabulary (ADR-027). This
    branch is **delegated in full** to the Vocabulary service (M3 task 7 / ADR-035): approve mutates
    the live vocabulary + opens the ``vocab-consolidation`` job, reject discards. Governance lives
    at one choke point shared with ``PUT /settings/vocabulary`` (ADR-027 §4), which owns its own
    status transition — so this service just hands the item over (:class:`VocabGovernance`).

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
from typing import Protocol
from zoneinfo import ZoneInfo

from ..config import Settings
from ..entities.resolver import significant_tokens
from ..entities.store import normalize_alias
from ..graph.node_writer import NodeDocument, NodeEdge, NodeWriter
from ..indexing.indexer import NodeIndexer
from ..indexing.store import IndexStore
from ..vocab.service import VocabularyProvider, effective_vocabulary
from .agent_runs import AgentRunStore
from .review_queue import (
    DECIDABLE_STATUSES,
    KIND_ENTITY_AMBIGUITY,
    KIND_STANCE_CANDIDATE,
    KIND_VOCAB_PROPOSAL,
    STATUS_DISCARDED,
    STATUS_MAYBE,
    STATUS_RESOLVED,
    BadResolution,
    ReviewNotFound,
    ReviewNotPending,
    ReviewReadStore,
    ReviewRecord,
)
from .store_backup import StoreBackup

logger = logging.getLogger(__name__)

# Re-exported for the review router, which imports the resolution exceptions from here; they now
# live in review_queue (shared with the Vocabulary service). Keep the names importable.
__all__ = ["ReviewService", "ReviewNotFound", "ReviewNotPending", "BadResolution"]


class VocabGovernance(VocabularyProvider, Protocol):
    """What the Review service needs from the Vocabulary service (task 7 / ADR-027 §4).

    Two things: delegate the whole ``vocab-proposal`` branch (mutate the live vocabulary + open the
    consolidation job + own the status transition) via :meth:`resolve_proposal`, and read the
    **effective** entity-like types (inherited :meth:`effective`) so minting a ``new`` entity of a
    freshly-approved type is accepted. Both are satisfied by the one ``VocabularyService``."""

    async def resolve_proposal(self, review_id: str, verdict: str | None) -> ReviewRecord: ...


class ChatCaptureIngest(Protocol):
    """The one capture-pipeline method the ``stance-candidate`` **agree** path needs: materialize an
    endorsed candidate as a ``source=chat`` capture that flows through the organizer (ADR-048 §7).

    Agree reuses **the exact auto-endorse path** the chat-distiller uses (one ingest path, not two),
    so a review-agreed memory is indistinguishable downstream from an auto-endorsed one and is
    replayed by ``reprocess-all`` (P10). Declared here (not imported from ``app.chat``) so the
    services layer needn't depend on the chat package — the concrete impl is ``CapturePipeline``."""

    async def create_chat_capture(
        self, text: str, *, session_id: str, created_at: datetime
    ) -> str: ...


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
        vocab: VocabGovernance | None = None,
        chat_ingest: ChatCaptureIngest | None = None,
    ) -> None:
        self._settings = settings
        self._store = review_store
        self._index = index_store
        self._indexer = indexer
        self._writer = node_writer
        self._backup = store_backup
        self._runs = run_store
        # Vocabulary governance (task 7): delegates the vocab-proposal branch + supplies the
        # effective entity-like types for minting. None ⇒ vocab-proposals unresolvable + seed-only
        # entity types (existing task-4 tests construct without it).
        self._vocab = vocab
        # Capture pipeline (M6 task 2): the stance-candidate **agree** path materializes a
        # `source=chat` capture through it — the exact auto-endorse path (ADR-048 §7). None ⇒
        # stance-candidate agree unresolvable (older tests that don't file stance items).
        self._chat_ingest = chat_ingest
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

        Materialization (the entity edge, or the stance-candidate agree capture) runs before the
        status transition, so a materialization failure leaves the item decidable (retryable) rather
        than resolved-but-unapplied. Both are idempotent (the edge writes are; the agree capture has
        a deterministic id, ADR-048 §1), so the (single-user) race where the guarded transition then
        finds it already terminal is harmless.
        """
        record = await self._store.get(review_id)
        if record is None:
            raise ReviewNotFound(review_id)
        # Decidable = pending ∪ maybe (ADR-048 §7): a parked `maybe` re-opens to a later verdict;
        # only the terminal resolved/discarded raise 409.
        if record.status not in DECIDABLE_STATUSES:
            raise ReviewNotPending(review_id)

        if record.kind == KIND_VOCAB_PROPOSAL:
            # Vocabulary governance (mutate the live vocab + open the consolidation job) is the
            # Vocabulary service's concern; it owns its own status transition (ADR-027 §4 / task 7).
            if self._vocab is None:  # pragma: no cover — always wired in main.py
                raise BadResolution("vocab-proposal resolution is not configured")
            return await self._vocab.resolve_proposal(review_id, verdict)
        if record.kind == KIND_ENTITY_AMBIGUITY:
            new_status, resolution = await self._resolve_entity(record, choice)
        elif record.kind == KIND_STANCE_CANDIDATE:
            new_status, resolution = await self._resolve_stance_candidate(record, verdict)
        else:
            raise BadResolution(f"kind {record.kind!r} is not resolvable")
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
        await self._accrete_on_link(record, choice)  # ADR-040 §4 — review-resolution link path
        return STATUS_RESOLVED, {"choice": choice}

    async def _accrete_on_link(self, record: ReviewRecord, entity_id: str) -> None:
        """Accrete the mention's surface form onto the chosen hub's ``aliases`` (ADR-040 §4), so a
        human's disambiguation teaches the hub the variant for next time. Guarded (skip short/low-
        entropy forms) + idempotent (skip if already an alias); best-effort (rule 7)."""
        mention = record.payload.get("mention") or {}
        surface = str(mention.get("name") or "").strip()
        if not surface or not significant_tokens(
            surface,
            min_len=self._settings.entity_min_token_len,
            stop=set(self._settings.entity_stop_tokens),
        ):
            return
        candidate = next(
            (c for c in record.payload.get("candidates", []) if c.get("id") == entity_id), None
        )
        if candidate is None:
            return
        aliases = [a for a in (candidate.get("aliases") or []) if isinstance(a, str)]
        if normalize_alias(surface) in {normalize_alias(a) for a in aliases}:
            return
        state = await self._index.get_index_state(entity_id)
        if state is None:
            return
        try:
            await asyncio.to_thread(self._writer.set_aliases, state.store_path, [*aliases, surface])
            await self._indexer.index_paths([state.store_path])
            await self._backup.request_commit("review: accrete alias")
        except FileNotFoundError:
            logger.warning("review: hub %s gone; alias not accreted (skipped)", state.store_path)

    async def _mint_entity(self, record: ReviewRecord) -> tuple[str, str]:
        """Mint a thin entity hub for the ``new`` choice (title + alias, ADR-030 §4), then index."""
        mention = record.payload.get("mention") or {}
        name = str(mention.get("name") or "").strip()
        entity_type = str(mention.get("type") or "").strip()
        # Effective entity-like types (seeds ∪ approved additions): a type approved after this item
        # was filed is still mintable (ADR-027/035). None provider ⇒ seed-only fallback.
        entity_like = (await effective_vocabulary(self._vocab, self._settings)).entity_like_types
        if not name or entity_type not in entity_like:
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

    # --- stance-candidate (M6, ADR-048 §7) ----------------------------------------------

    async def _resolve_stance_candidate(
        self, record: ReviewRecord, verdict: str | None
    ) -> tuple[str, dict]:
        """Resolve a chat-distilled stance-unclear candidate (ADR-048 §7).

        ``agree`` → materialize a ``source=chat`` capture through the pipeline — **the exact
        auto-endorse path** (one ingest path, not two) — so it organizes + is replayed by
        ``reprocess-all`` (P10); ``disagree`` → discarded (logged, never a node); ``maybe`` → parked
        and re-openable. The capture's ``created_at`` is the anchoring message time recorded in the
        payload at file time (``anchor_at``), so an agreed memory carries *conversation* time, not
        the review-decision time — matching auto-endorse."""
        decision = (verdict or "").strip().lower()
        if decision == "maybe":
            return STATUS_MAYBE, {"verdict": "maybe"}
        if decision == "disagree":
            logger.info("stance-candidate %s disagreed → discarded (no node)", record.id)
            return STATUS_DISCARDED, {"verdict": "disagree"}
        if decision != "agree":
            raise BadResolution("stance-candidate requires a 'verdict' of agree|disagree|maybe")

        if self._chat_ingest is None:  # pragma: no cover — always wired in main.py
            raise BadResolution("stance-candidate agree is not configured")
        text = str(record.payload.get("candidate_text") or "").strip()
        session_id = (record.source_ref or "").strip()
        if not text or not session_id:
            raise BadResolution("stance-candidate is missing its candidate_text / session")
        capture_id = await self._chat_ingest.create_chat_capture(
            text, session_id=session_id, created_at=self._stance_anchor(record)
        )
        return STATUS_RESOLVED, {"verdict": "agree", "capture_id": capture_id}

    def _stance_anchor(self, record: ReviewRecord) -> datetime:
        """The ``created_at`` an agreed candidate's capture is stamped with: the anchoring message
        time the distiller recorded in the payload (``anchor_at``, ISO-8601). Falls back to the
        review item's own ``created_at`` if absent/unparseable (still a sane, monotonic time)."""
        raw = record.payload.get("anchor_at")
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                logger.warning("stance-candidate %s: unparseable anchor_at %r", record.id, raw)
        return record.created_at

    # --- entity-ambiguity materialization ------------------------------------------------

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
