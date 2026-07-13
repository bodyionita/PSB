"""Meta router tests: GET /planes + GET /types return the configured/effective vocabulary
(no DB, auth bypassed)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_settings, get_vocabulary_service, require_session
from app.routers import meta
from app.vocab.consolidation import VocabConsolidation
from app.vocab.service import VocabularyService

from .fakes import FakeAgentRunStore, FakeReviewQueue, FakeVocabularyStore

PREFIX = "/api/v1"


def _client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.include_router(meta.router, prefix=PREFIX)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app)


def _client_with_vocab(settings: Settings):
    app = FastAPI()
    app.include_router(meta.router, prefix=PREFIX)
    review = FakeReviewQueue()
    store = FakeVocabularyStore()
    service = VocabularyService(
        settings=settings,
        vocab_store=store,
        review_store=review,
        consolidation=VocabConsolidation(run_store=FakeAgentRunStore()),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_vocabulary_service] = lambda: service
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app), review, store


def test_planes_returns_configured_planes_and_inbox():
    settings = Settings(planes=["Work", "Home"], inbox_folder="inbox")
    resp = _client(settings).get(f"{PREFIX}/planes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["planes"] == ["Work", "Home"]
    assert body["inbox"] == "inbox"


def test_planes_empty_config_still_returns_inbox():
    settings = Settings(planes=[], inbox_folder="inbox")
    body = _client(settings).get(f"{PREFIX}/planes").json()
    assert body["planes"] == []
    assert body["inbox"] == "inbox"


def test_types_returns_effective_vocab_with_approved_additions():
    settings = Settings(
        node_types=["memory", "person"],
        edge_rels=["involves"],
        entity_like_types=["person"],
    )
    client, _review, store = _client_with_vocab(settings)
    store.node_types = ["dream"]  # an approved addition (seeds ∪ additions, seeds first)

    resp = client.get(f"{PREFIX}/types")
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_types"] == ["memory", "person", "dream"]
    assert body["edge_rels"] == ["involves"]
    assert body["entity_like_types"] == ["person"]
    assert body["proposals"] == []  # none pending
