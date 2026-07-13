"""Search router tests: fake service via dependency override (no DB, no LLM, auth bypassed)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_search_service, require_session
from app.providers.base import ProviderUnavailable
from app.routers import search
from app.search.service import NotePreview
from app.search.store import RelatedNote, SearchHit

PREFIX = "/api/v1"

_HIT = SearchHit(
    note_id="11111111-1111-1111-1111-111111111111",
    vault_path="Ideas/x.md", title="X", plane="Ideas", planes=["Ideas"],
    tags=["t"], snippet="a snippet", score=0.87,
)


class FakeSearchService:
    def __init__(self, *, hits=None, preview=None, embed_down=False):
        self._hits = hits or []
        self._preview = preview
        self._embed_down = embed_down
        self.calls: list[dict] = []

    async def search(self, query, *, top_k=None, planes=None):
        self.calls.append({"query": query, "top_k": top_k, "planes": planes})
        if self._embed_down:
            raise ProviderUnavailable("embedder down")
        return list(self._hits)

    async def get_note(self, note_id):
        if self._preview is not None and self._preview.note_id == note_id:
            return self._preview
        return None


def _client(service: FakeSearchService) -> TestClient:
    app = FastAPI()
    app.include_router(search.router, prefix=PREFIX)
    app.dependency_overrides[get_search_service] = lambda: service
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app)


def test_search_returns_note_grouped_hits():
    service = FakeSearchService(hits=[_HIT])
    resp = _client(service).post(f"{PREFIX}/search", json={"query": "pricing", "top_k": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["note_id"] == _HIT.note_id
    assert body[0]["snippet"] == "a snippet"
    assert body[0]["score"] == pytest.approx(0.87)
    assert service.calls == [{"query": "pricing", "top_k": 5, "planes": None}]


def test_search_missing_query_is_422():
    resp = _client(FakeSearchService()).post(f"{PREFIX}/search", json={"top_k": 5})
    assert resp.status_code == 422


def test_search_embedder_down_is_503():
    resp = _client(FakeSearchService(embed_down=True)).post(f"{PREFIX}/search", json={"query": "q"})
    assert resp.status_code == 503


def test_get_note_returns_preview_with_related():
    nid = "11111111-1111-1111-1111-111111111111"
    preview = NotePreview(
        note_id=nid, vault_path="Ideas/x.md", title="X", plane="Ideas", planes=["Ideas"],
        tags=["t"], body="# X\n\nbody",
        related=[RelatedNote(note_id="22222222-2222-2222-2222-222222222222",
                             vault_path="Ideas/y.md", title="Y", score=0.7)],
    )
    resp = _client(FakeSearchService(preview=preview)).get(f"{PREFIX}/notes/{nid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["body"] == "# X\n\nbody"
    assert body["related"][0]["note_id"] == "22222222-2222-2222-2222-222222222222"


def test_get_note_unknown_is_404():
    nid = str(uuid.uuid4())
    resp = _client(FakeSearchService(preview=None)).get(f"{PREFIX}/notes/{nid}")
    assert resp.status_code == 404


def test_get_note_malformed_uuid_is_422():
    resp = _client(FakeSearchService()).get(f"{PREFIX}/notes/not-a-uuid")
    assert resp.status_code == 422
