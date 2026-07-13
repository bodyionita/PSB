"""Settings router tests: PUT /settings/vocabulary approve/reject + error shapes (ADR-027 / ADR-035,
M3 task 7). No DB — a real VocabularyService over fakes, auth bypassed."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_vocabulary_service, require_session
from app.routers import settings as settings_router
from app.services.review_queue import KIND_ENTITY_AMBIGUITY, KIND_VOCAB_PROPOSAL, ReviewItem
from app.vocab.consolidation import VocabConsolidation
from app.vocab.service import VocabularyService

from .fakes import FakeAgentRunStore, FakeReviewQueue, FakeVocabularyStore

PREFIX = "/api/v1"


def _client():
    settings = Settings(
        node_types=["memory", "person"], edge_rels=["involves"], entity_like_types=["person"]
    )
    review = FakeReviewQueue()
    store = FakeVocabularyStore()
    service = VocabularyService(
        settings=settings,
        vocab_store=store,
        review_store=review,
        consolidation=VocabConsolidation(run_store=FakeAgentRunStore()),
    )
    app = FastAPI()
    app.include_router(settings_router.router, prefix=PREFIX)
    app.dependency_overrides[get_vocabulary_service] = lambda: service
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app), review, store


def _seed(review: FakeReviewQueue, *, vocab: str = "node_type", value: str = "dream") -> str:
    item = ReviewItem(kind=KIND_VOCAB_PROPOSAL, payload={"vocab": vocab, "value": value})
    return asyncio.run(review.enqueue(item))


def _put(client: TestClient, review_id: str, verdict: str):
    return client.put(
        f"{PREFIX}/settings/vocabulary", json={"review_id": review_id, "verdict": verdict}
    )


def test_approve_resolves_and_makes_type_live():
    client, review, store = _client()
    rid = _seed(review, vocab="node_type", value="dream")
    resp = _put(client, rid, "approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["resolution"]["value"] == "dream"
    assert "dream" in store.node_types


def test_reject_discards():
    client, review, store = _client()
    rid = _seed(review, vocab="edge_rel", value="x")
    resp = _put(client, rid, "reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "discarded"
    assert store.edge_rels == []


def test_unknown_proposal_is_404():
    client, *_ = _client()
    assert _put(client, "nope", "approve").status_code == 404


def test_already_resolved_is_409():
    client, review, _ = _client()
    rid = _seed(review)
    _put(client, rid, "reject")
    assert _put(client, rid, "approve").status_code == 409


def test_bad_verdict_is_400():
    client, review, _ = _client()
    rid = _seed(review)
    assert _put(client, rid, "meh").status_code == 400


def test_non_vocab_item_is_400():
    client, review, _ = _client()
    rid = asyncio.run(review.enqueue(ReviewItem(kind=KIND_ENTITY_AMBIGUITY, payload={})))
    assert _put(client, rid, "approve").status_code == 400


def test_missing_body_field_is_422():
    client, *_ = _client()
    resp = client.put(f"{PREFIX}/settings/vocabulary", json={"review_id": "x"})
    assert resp.status_code == 422  # verdict is required
