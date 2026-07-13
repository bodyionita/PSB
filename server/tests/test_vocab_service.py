"""Vocabulary governance tests (ADR-027 / ADR-035, M3 task 7a): effective vocabulary + the shared
approve/reject choke point, with fakes (no live DB/LLM — 08 testing policy).

Covers: seeds ∪ approved additions (forward-live), ``GET /types`` listing, and ``resolve_proposal``
for all three axes (node_type / entity_type / edge_rel) + the error shapes the routers map. The
approved-vocabulary store's pure helpers (dedup/decode) are exercised too.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.review_queue import (
    KIND_ENTITY_AMBIGUITY,
    KIND_VOCAB_PROPOSAL,
    BadResolution,
    ReviewItem,
    ReviewNotFound,
    ReviewNotPending,
)
from app.vocab.consolidation import AGENT as CONSOLIDATION_AGENT
from app.vocab.consolidation import VocabConsolidation
from app.vocab.service import EffectiveVocabulary, VocabularyService, effective_vocabulary
from app.vocab.store import VocabularyAdditions, _decode, _merge

from .fakes import FakeAgentRunStore, FakeReviewQueue, FakeVocabularyStore


def _settings() -> Settings:
    return Settings(
        node_types=["memory", "person", "idea"],
        edge_rels=["involves", "about"],
        entity_like_types=["person", "idea"],
        scheduler_tz="UTC",
    )


def _service(settings: Settings | None = None):
    settings = settings or _settings()
    review = FakeReviewQueue()
    store = FakeVocabularyStore()
    runs = FakeAgentRunStore()
    service = VocabularyService(
        settings=settings,
        vocab_store=store,
        review_store=review,
        consolidation=VocabConsolidation(run_store=runs),
    )
    return service, review, store, runs


# --- effective vocabulary -----------------------------------------------------------------


async def test_effective_helper_falls_back_to_seeds_without_provider():
    settings = _settings()
    eff = await effective_vocabulary(None, settings)
    assert eff == EffectiveVocabulary.from_settings(settings)
    assert eff.node_types == ("memory", "person", "idea")


async def test_effective_unions_seeds_and_approved_additions_order_preserving():
    service, _, store, _ = _service()
    store.node_types = ["dream", "memory"]  # 'memory' overlaps a seed → deduped, seeds first
    store.edge_rels = ["mentors"]
    eff = await service.effective()
    assert eff.node_types == ("memory", "person", "idea", "dream")
    assert eff.edge_rels == ("involves", "about", "mentors")


# --- GET /types ---------------------------------------------------------------------------


async def test_list_types_reports_effective_plus_pending_proposals():
    service, review, store, _ = _service()
    store.node_types = ["dream"]
    await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "edge_rel", "value": "mentors"})
    )
    # A resolved vocab item + a non-vocab item must not appear as pending proposals.
    other = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "old"})
    )
    await review.resolve(other, status="discarded", resolution={"verdict": "reject"})
    await review.enqueue(ReviewItem(kind=KIND_ENTITY_AMBIGUITY, payload={}))

    view = await service.list_types()
    assert "dream" in view.node_types
    assert len(view.proposals) == 1
    assert view.proposals[0]["vocab"] == "edge_rel"
    assert view.proposals[0]["value"] == "mentors"


# --- resolve_proposal: approve --------------------------------------------------------------


async def test_approve_node_type_makes_it_live_and_opens_run():
    service, review, store, runs = _service()
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "dream"})
    )

    record = await service.resolve_proposal(rid, "approve")

    assert record.status == "resolved"
    assert record.resolution["value"] == "dream"
    # Written to app_settings (node_types only — not an entity type).
    assert "dream" in store.node_types
    assert "dream" not in store.entity_like_types
    # Forward-live: the effective vocabulary now includes it.
    assert "dream" in (await service.effective()).node_types
    # A visible SUCCEEDED consolidation run was opened and referenced in the resolution.
    run = next(r for r in runs.runs.values() if r.agent == CONSOLIDATION_AGENT)
    assert run.status == "succeeded"
    assert record.resolution["run_id"] == run.id


async def test_approve_entity_type_extends_both_axes():
    service, review, store, _ = _service()
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "entity_type", "value": "pet"})
    )
    await service.resolve_proposal(rid, "approve")
    eff = await service.effective()
    assert "pet" in eff.node_types  # an entity-like type is also a node type (ADR-030)
    assert "pet" in eff.entity_like_types


async def test_approve_edge_rel_extends_edges():
    service, review, store, _ = _service()
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "edge_rel", "value": "mentors"})
    )
    await service.resolve_proposal(rid, "approve")
    assert "mentors" in store.edge_rels
    assert "mentors" in (await service.effective()).edge_rels


async def test_approve_is_idempotent_on_repeat_value():
    service, review, store, _ = _service()
    for _ in range(2):
        rid = await review.enqueue(
            ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "dream"})
        )
        await service.resolve_proposal(rid, "approve")
    assert store.node_types.count("dream") == 1


# --- resolve_proposal: reject + errors ------------------------------------------------------


async def test_reject_discards_and_opens_no_run():
    service, review, store, runs = _service()
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "dream"})
    )
    record = await service.resolve_proposal(rid, "reject")
    assert record.status == "discarded"
    assert store.node_types == []
    assert not runs.runs


async def test_bad_verdict_rejected():
    service, review, *_ = _service()
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "dream"})
    )
    with pytest.raises(BadResolution):
        await service.resolve_proposal(rid, "maybe")


async def test_non_vocab_kind_rejected():
    service, review, *_ = _service()
    rid = await review.enqueue(ReviewItem(kind=KIND_ENTITY_AMBIGUITY, payload={}))
    with pytest.raises(BadResolution):
        await service.resolve_proposal(rid, "approve")


async def test_missing_value_rejected_and_nothing_mutated():
    service, review, store, runs = _service()
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "  "})
    )
    with pytest.raises(BadResolution):
        await service.resolve_proposal(rid, "approve")
    assert store.node_types == [] and not runs.runs


async def test_unknown_id_not_found():
    service, *_ = _service()
    with pytest.raises(ReviewNotFound):
        await service.resolve_proposal("nope", "approve")


async def test_already_resolved_conflicts():
    service, review, *_ = _service()
    rid = await review.enqueue(
        ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": "node_type", "value": "dream"})
    )
    await service.resolve_proposal(rid, "reject")
    with pytest.raises(ReviewNotPending):
        await service.resolve_proposal(rid, "approve")


# --- store pure helpers ---------------------------------------------------------------------


def test_merge_dedups_and_preserves_order():
    assert _merge(("a", "b"), ["b", " c ", "", "a", "d"]) == ["a", "b", "c", "d"]


def test_decode_tolerates_null_and_off_shape_values():
    # app_settings.value is jsonb (always valid JSON); decode still guards non-list / non-str items.
    assert _decode(None) == VocabularyAdditions()
    assert _decode("[]") == VocabularyAdditions()  # a non-dict json value
    decoded = _decode('{"node_types": ["x", "x", 3], "edge_rels": "nope"}')
    assert decoded.node_types == ("x",)  # dupes + non-str dropped
    assert decoded.edge_rels == ()  # non-list dropped
