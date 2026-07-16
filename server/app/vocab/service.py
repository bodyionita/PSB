"""Vocabulary service (ADR-027 / ADR-035, M3 task 7).

Two responsibilities, one choke point (ADR-027 §4 — governance enforceable in one place):

  * **The effective vocabulary.** :class:`EffectiveVocabulary` = config seeds ∪ approved additions
    (:mod:`app.vocab.store`). Every writer that types a node/edge reads it, so an approved type is
    **forward-live at once** (the organizer, ``validate_organizer_output``, ``GET /types`` and the
    entity-substrate readers). :func:`effective_vocabulary` is the accessor callers use; passing
    ``None`` for the provider falls back to the raw settings seeds (keeps existing services' tests
    unchanged — they construct without a provider).
  * **Resolving a vocab proposal.** :meth:`VocabularyService.resolve_proposal` is the shared
    approve/reject logic behind both ``PUT /settings/vocabulary`` and ``POST /review/{id}`` (the
    kind-generic Review queue). Approve → write the addition to ``app_settings`` (mutate the live
    vocabulary) → open the ``vocab-consolidation`` job (ADR-035); reject → discard. It owns the
    review-item status transition, so the Review service delegates the whole vocab branch here.

Depends on protocols (vocab store, review store, a consolidation launcher) so it unit-tests against
fakes — no live DB/LLM (08 testing policy).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from ..config import Settings
from ..services.review_queue import (
    KIND_VOCAB_PROPOSAL,
    STATUS_DISCARDED,
    STATUS_PENDING,
    STATUS_RESOLVED,
    BadResolution,
    ReviewNotFound,
    ReviewNotPending,
    ReviewReadStore,
    ReviewRecord,
)
from .store import VocabularyStore

logger = logging.getLogger(__name__)

# The proposal ``vocab`` axes the organizer files (organizer._propose): a plain node type, an
# entity-like node type (carries the entity substrate — ADR-030), or an edge relation.
VOCAB_NODE_TYPE = "node_type"
VOCAB_ENTITY_TYPE = "entity_type"
VOCAB_EDGE_REL = "edge_rel"
_VOCAB_AXES = (VOCAB_NODE_TYPE, VOCAB_ENTITY_TYPE, VOCAB_EDGE_REL)


@dataclass(frozen=True)
class EffectiveVocabulary:
    """The live vocabulary a writer sees: config seeds ∪ user-approved additions, per axis."""

    node_types: tuple[str, ...]
    edge_rels: tuple[str, ...]
    entity_like_types: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> EffectiveVocabulary:
        """The seeds-only vocabulary (no approved additions) — the ``None``-provider fallback."""
        return cls(
            node_types=tuple(settings.node_types),
            edge_rels=tuple(settings.edge_rels),
            entity_like_types=tuple(settings.entity_like_types),
        )


class VocabularyProvider(Protocol):
    """The narrow read the writers depend on — the current effective vocabulary."""

    async def effective(self) -> EffectiveVocabulary: ...


class ConsolidationLauncher(Protocol):
    """What an approval needs from the consolidation job: open a ``vocab-consolidation`` run for the
    newly approved type and return its id (best-effort — must never fail the approval, rule 7)."""

    async def start(self, *, vocab: str, value: str, review_id: str) -> str | None: ...


async def effective_vocabulary(
    vocab: VocabularyProvider | None, settings: Settings
) -> EffectiveVocabulary:
    """The effective vocabulary from the provider, or seeds-only when no provider is wired.

    Every writer reads its vocabulary through this so a newly approved type is forward-live; the
    ``None`` path keeps services constructed without a provider (existing unit tests) on the seeds.
    """
    if vocab is not None:
        return await vocab.effective()
    return EffectiveVocabulary.from_settings(settings)


@dataclass(frozen=True)
class TypesView:
    """``GET /types`` payload: the effective vocabulary + the still-pending type proposals."""

    node_types: tuple[str, ...]
    edge_rels: tuple[str, ...]
    entity_like_types: tuple[str, ...]
    proposals: tuple[dict, ...]


class VocabularyService:
    """Effective-vocabulary provider + the shared vocab-proposal approve/reject choke point."""

    def __init__(
        self,
        *,
        settings: Settings,
        vocab_store: VocabularyStore,
        review_store: ReviewReadStore | None = None,
        consolidation: ConsolidationLauncher | None = None,
    ) -> None:
        # `review_store`/`consolidation` are only used by the approve/reject + list_types paths;
        # `effective()` needs just settings + vocab_store. They default to None so a standalone
        # caller (the CLI run-now) can build this purely as a VocabularyProvider (ADR-027
        # forward-live vocab) without wiring the review queue.
        self._settings = settings
        self._store = vocab_store
        self._review = review_store
        self._consolidation = consolidation

    # --- effective vocabulary (VocabularyProvider) ------------------------------------------

    async def effective(self) -> EffectiveVocabulary:
        """Config seeds ∪ approved additions, per axis (seeds first, additions appended)."""
        additions = await self._store.get_additions()
        return EffectiveVocabulary(
            node_types=_union(self._settings.node_types, additions.node_types),
            edge_rels=_union(self._settings.edge_rels, additions.edge_rels),
            entity_like_types=_union(self._settings.entity_like_types, additions.entity_like_types),
        )

    # --- GET /types -------------------------------------------------------------------------

    async def list_types(self) -> TypesView:
        """The effective vocabulary + pending ``vocab-proposal`` items (ADR-027, ``GET /types``)."""
        effective = await self.effective()
        pending = await self._review.list_items(
            status=STATUS_PENDING, kind=KIND_VOCAB_PROPOSAL, limit=self._settings.review_list_max
        )
        proposals = tuple(
            {
                "id": item.id,
                "vocab": item.payload.get("vocab"),
                "value": item.payload.get("value"),
                "excerpt": item.excerpt,
                "created_at": item.created_at.isoformat(),
            }
            for item in pending
        )
        return TypesView(
            node_types=effective.node_types,
            edge_rels=effective.edge_rels,
            entity_like_types=effective.entity_like_types,
            proposals=proposals,
        )

    # --- approve / reject (PUT /settings/vocabulary + POST /review/{id}) ---------------------

    async def resolve_proposal(self, review_id: str, verdict: str | None) -> ReviewRecord:
        """Approve or reject a ``vocab-proposal`` item; returns the updated record.

        Approve mutates the live vocabulary (``app_settings``) **before** opening the consolidation
        job and **before** the status transition, so a store-write failure leaves the item pending
        (retryable) rather than resolved-but-unapplied. Raises :class:`ReviewNotFound` (404),
        :class:`ReviewNotPending` (409), or :class:`BadResolution` (400) for the router to map.
        """
        record = await self._review.get(review_id)
        if record is None:
            raise ReviewNotFound(review_id)
        if record.kind != KIND_VOCAB_PROPOSAL:
            raise BadResolution(f"review item {review_id} is not a vocab proposal")
        if record.status != STATUS_PENDING:
            raise ReviewNotPending(review_id)

        if verdict == "reject":
            await self._review.resolve(
                review_id, status=STATUS_DISCARDED, resolution={"verdict": "reject"}
            )
            return await self._reload(review_id, record)
        if verdict != "approve":
            raise BadResolution("vocab-proposal requires a 'verdict' of 'approve' or 'reject'")

        vocab, value = _proposal_fields(record)
        await self._apply_addition(vocab, value)
        run_id = await self._consolidation.start(vocab=vocab, value=value, review_id=record.id)
        await self._review.resolve(
            review_id,
            status=STATUS_RESOLVED,
            resolution={"verdict": "approve", "vocab": vocab, "value": value, "run_id": run_id},
        )
        return await self._reload(review_id, record)

    async def _apply_addition(self, vocab: str, value: str) -> None:
        """Write the approved type to ``app_settings`` (idempotent). An entity-like type is also a
        node type (ADR-030), so approving one extends both axes; boot guards
        (``ENTITY_LIKE_TYPES ⊆ NODE_TYPES``) therefore keep holding for the effective sets."""
        if vocab == VOCAB_NODE_TYPE:
            await self._store.add(node_types=[value])
        elif vocab == VOCAB_ENTITY_TYPE:
            await self._store.add(node_types=[value], entity_like_types=[value])
        elif vocab == VOCAB_EDGE_REL:
            await self._store.add(edge_rels=[value])
        else:  # pragma: no cover — guarded by _proposal_fields
            raise BadResolution(f"unknown vocab axis {vocab!r}")

    async def _reload(self, review_id: str, fallback: ReviewRecord) -> ReviewRecord:
        updated = await self._review.get(review_id)
        return updated if updated is not None else fallback


def _proposal_fields(record: ReviewRecord) -> tuple[str, str]:
    """Extract + validate ``(vocab, value)`` from a proposal payload (400 on anything unusable)."""
    vocab = record.payload.get("vocab")
    value = record.payload.get("value")
    if vocab not in _VOCAB_AXES:
        raise BadResolution(f"proposal has no known vocab axis (got {vocab!r})")
    if not isinstance(value, str) or not value.strip():
        raise BadResolution("proposal has no usable 'value' to approve")
    return vocab, value.strip()


def _union(seeds: list[str] | tuple[str, ...], additions: tuple[str, ...]) -> tuple[str, ...]:
    """Seeds then additions, de-duplicated, order-preserving."""
    out: list[str] = []
    for value in (*seeds, *additions):
        if value not in out:
            out.append(value)
    return tuple(out)
