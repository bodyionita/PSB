"""Search & graph router tests: fake service via dependency override (no DB, no LLM, auth off)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_search_service, require_session
from app.providers.base import ProviderUnavailable
from app.routers import search
from app.search.service import NodePreview
from app.search.store import NodeEdgeView, SearchHit

PREFIX = "/api/v1"

_HIT = SearchHit(
    node_id="11111111-1111-1111-1111-111111111111",
    store_path="memory/x.md",
    type="memory",
    title="X",
    plane="Ideas",
    planes=["Ideas"],
    tags=["t"],
    snippet="a snippet",
    score=0.87,
)


def _preview(node_id: str, *, merged_into=None) -> NodePreview:
    return NodePreview(
        node_id=node_id,
        store_path="memory/x.md",
        type="memory",
        title="X",
        plane="Ideas",
        planes=["Ideas"],
        tags=["t"],
        aliases=[],
        disambig=None,
        occurred=None,
        occurred_end=None,
        body="# X\n\nbody",
        profile=None,
        merged_into=merged_into,
        edges=[
            NodeEdgeView(
                rel="involves",
                dir="out",
                node_id="22222222-2222-2222-2222-222222222222",
                type="person",
                title="Alex",
                origin="canonical",
                score=None,
                since=None,
                until=None,
            )
        ],
    )


class FakeSearchService:
    def __init__(self, *, hits=None, preview=None, embed_down=False):
        self._hits = hits or []
        self._preview = preview
        self._embed_down = embed_down
        self.calls: list[dict] = []

    async def search(self, query, *, top_k=None, planes=None, types=None):
        self.calls.append({"query": query, "top_k": top_k, "planes": planes, "types": types})
        if self._embed_down:
            raise ProviderUnavailable("embedder down")
        return list(self._hits)

    async def get_node(self, node_id):
        if self._preview is not None and self._preview.node_id == node_id:
            return self._preview
        return None


def _client(service: FakeSearchService) -> TestClient:
    app = FastAPI()
    app.include_router(search.router, prefix=PREFIX)
    app.dependency_overrides[get_search_service] = lambda: service
    app.dependency_overrides[require_session] = lambda: None  # bypass auth
    return TestClient(app)


def test_search_returns_node_grouped_hits():
    service = FakeSearchService(hits=[_HIT])
    resp = _client(service).post(
        f"{PREFIX}/search", json={"query": "pricing", "top_k": 5, "types": ["memory"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["node_id"] == _HIT.node_id
    assert body[0]["type"] == "memory"
    assert body[0]["snippet"] == "a snippet"
    assert body[0]["score"] == pytest.approx(0.87)
    assert service.calls == [{"query": "pricing", "top_k": 5, "planes": None, "types": ["memory"]}]


def test_search_missing_query_is_422():
    resp = _client(FakeSearchService()).post(f"{PREFIX}/search", json={"top_k": 5})
    assert resp.status_code == 422


def test_search_embedder_down_is_503():
    resp = _client(FakeSearchService(embed_down=True)).post(f"{PREFIX}/search", json={"query": "q"})
    assert resp.status_code == 503


def test_get_node_returns_detail_with_edges():
    nid = "11111111-1111-1111-1111-111111111111"
    resp = _client(FakeSearchService(preview=_preview(nid))).get(f"{PREFIX}/nodes/{nid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "memory"
    assert body["body"] == "# X\n\nbody"
    assert body["edges"][0]["node_id"] == "22222222-2222-2222-2222-222222222222"
    assert body["edges"][0]["dir"] == "out"


def test_get_node_tombstone_redirects_to_survivor():
    nid = "11111111-1111-1111-1111-111111111111"
    survivor = "99999999-9999-9999-9999-999999999999"
    client = _client(FakeSearchService(preview=_preview(nid, merged_into=survivor)))
    resp = client.get(f"{PREFIX}/nodes/{nid}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith(f"/nodes/{survivor}")


def test_get_node_unknown_is_404():
    nid = str(uuid.uuid4())
    resp = _client(FakeSearchService(preview=None)).get(f"{PREFIX}/nodes/{nid}")
    assert resp.status_code == 404


def test_get_node_malformed_uuid_is_422():
    resp = _client(FakeSearchService()).get(f"{PREFIX}/nodes/not-a-uuid")
    assert resp.status_code == 422
