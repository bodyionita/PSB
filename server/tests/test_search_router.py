"""Search & graph router tests: fake service via dependency override (no DB, no LLM, auth off)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_graph_service, get_search_service, require_session
from app.graph.service import GraphService
from app.graph.store import NeighborEdge, NeighborHeader
from app.providers.base import ProviderUnavailable
from app.routers import search
from app.search.service import NodePreview
from app.search.store import NodeEdgeView, SearchHit
from app.services.node_media_store import NodeMediaItem

from .fakes import FakeEntityStore, FakeNeighborStore, FakeNodeReader

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


def _preview(node_id: str, *, merged_into=None, interiority="internal") -> NodePreview:
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
        interiority=interiority,
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

    async def search(
        self, query, *, top_k=None, planes=None, types=None, since=None, until=None, as_of=None
    ):
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "planes": planes,
                "types": types,
                "since": since,
                "until": until,
                "as_of": as_of,
            }
        )
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
    assert service.calls == [
        {
            "query": "pricing",
            "top_k": 5,
            "planes": None,
            "types": ["memory"],
            "since": None,
            "until": None,
            "as_of": None,
        }
    ]


def test_search_forwards_temporal_filters():
    service = FakeSearchService(hits=[_HIT])
    resp = _client(service).post(
        f"{PREFIX}/search",
        json={
            "query": "pricing",
            "since": "2026-01-01",
            "until": "2026-06-30",
            "as_of": "2026-03-15",
        },
    )
    assert resp.status_code == 200
    call = service.calls[0]
    assert (str(call["since"]), str(call["until"]), str(call["as_of"])) == (
        "2026-01-01",
        "2026-06-30",
        "2026-03-15",
    )


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


def test_search_exposes_media_kinds():
    # M9 T4 (ADR-060 §7): a hit's `media_kinds` (off the node_media link) rides the result card as
    # the glyph source. Empty for a node with no media.
    hit = SearchHit(
        node_id="11111111-1111-1111-1111-111111111111",
        store_path="memory/x.md",
        type="memory",
        title="X",
        plane="Ideas",
        planes=["Ideas"],
        tags=["t"],
        snippet="s",
        score=0.5,
        media_kinds=["photo", "voice"],
    )
    resp = _client(FakeSearchService(hits=[hit])).post(f"{PREFIX}/search", json={"query": "q"})
    assert resp.status_code == 200
    assert resp.json()[0]["media_kinds"] == ["photo", "voice"]
    # A hit with no media defaults to [].
    resp2 = _client(FakeSearchService(hits=[_HIT])).post(f"{PREFIX}/search", json={"query": "q"})
    assert resp2.json()[0]["media_kinds"] == []


def test_get_node_exposes_media():
    # M9 T4 (ADR-060 §1): `GET /nodes/{id}` carries the node's media strip (id, kind, status,
    # capture_id) so NodePreview can render photos/voice inline + open "see raw capture".
    nid = "11111111-1111-1111-1111-111111111111"
    preview = _preview(nid)
    object.__setattr__(
        preview,
        "media",
        [NodeMediaItem(id="m-1", kind="photo", status="derived", capture_id="cap-1")],
    )
    resp = _client(FakeSearchService(preview=preview)).get(f"{PREFIX}/nodes/{nid}")
    assert resp.status_code == 200
    media = resp.json()["media"]
    assert media == [{"id": "m-1", "kind": "photo", "status": "derived", "capture_id": "cap-1"}]


def test_get_node_exposes_interiority():
    # M8.2 T3.5: `interiority` is passed through NodeDetailResponse from the NodePreview (ADR-055
    # §3c — drives the web inner-voice marker). Null on an unstamped hub.
    nid = "11111111-1111-1111-1111-111111111111"
    resp = _client(FakeSearchService(preview=_preview(nid, interiority="internal"))).get(
        f"{PREFIX}/nodes/{nid}"
    )
    assert resp.status_code == 200
    assert resp.json()["interiority"] == "internal"

    resp2 = _client(FakeSearchService(preview=_preview(nid, interiority=None))).get(
        f"{PREFIX}/nodes/{nid}"
    )
    assert resp2.json()["interiority"] is None


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


# --- GET /entities (M9.8 T2, ADR-064 §2 — the merge picker's browse) ------------------------


def _entities_client(refs) -> TestClient:
    from app.dependencies import get_entity_browse_service
    from app.entities.entity_browse import EntityBrowseService

    store = FakeEntityStore(entities=refs)
    svc = EntityBrowseService(
        store=store, entity_like_types=["person", "place", "topic", "event", "project"]
    )
    app = FastAPI()
    app.include_router(search.router, prefix=PREFIX)
    app.dependency_overrides[get_entity_browse_service] = lambda: svc
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def _entity_ref(id: str, title: str | None, *, type: str = "person", aliases=None):
    from app.entities.entity_store import EntityRef

    return EntityRef(
        id=id, type=type, title=title, aliases=list(aliases or []), store_path=f"person/{id}.md"
    )


def test_browse_entities_search_by_name_resolves_id_and_shape():
    refs = [
        _entity_ref("11111111-1111-1111-1111-111111111111", "Diana Vance", aliases=["Di"]),
        _entity_ref("22222222-2222-2222-2222-222222222222", "Diana Wren"),
        _entity_ref("33333333-3333-3333-3333-333333333333", "Bob"),
    ]
    resp = _entities_client(refs).get(f"{PREFIX}/entities", params={"q": "diana vance"})
    assert resp.status_code == 200
    body = resp.json()
    assert body[0] == {
        "id": "11111111-1111-1111-1111-111111111111",
        "type": "person",
        "title": "Diana Vance",
        "aliases": ["Di"],
    }
    assert "33333333-3333-3333-3333-333333333333" not in [b["id"] for b in body]


def test_browse_entities_empty_query_is_alphabetical_browse():
    refs = [_entity_ref("b", "Zoe"), _entity_ref("a", "Alex")]
    resp = _entities_client(refs).get(f"{PREFIX}/entities")
    assert resp.status_code == 200
    assert [b["title"] for b in resp.json()] == ["Alex", "Zoe"]


def test_browse_entities_type_filter_narrows():
    refs = [
        _entity_ref("p", "Diana", type="person"),
        _entity_ref("t", "Diana", type="topic"),
    ]
    resp = _entities_client(refs).get(f"{PREFIX}/entities", params={"type": "topic"})
    assert resp.status_code == 200
    assert [b["id"] for b in resp.json()] == ["t"]


def test_browse_entities_limit_bounds_are_validated():
    client = _entities_client([_entity_ref("a", "A")])
    assert client.get(f"{PREFIX}/entities", params={"limit": 0}).status_code == 422
    assert client.get(f"{PREFIX}/entities", params={"limit": 51}).status_code == 422


# --- GET /nodes/{id}/neighbors (M7 map, ADR-051 §2) -----------------------------------------

_C1 = "11111111-1111-1111-1111-111111111111"
_N1 = "22222222-2222-2222-2222-222222222222"
_N2 = "33333333-3333-3333-3333-333333333333"


def _nedge(
    node_id: str, *, origin="canonical", rel="involves", dir="out", interiority=None
) -> NeighborEdge:
    return NeighborEdge(
        origin=origin,
        rel=rel,
        dir=dir,
        node_id=node_id,
        type="person",
        title="N",
        plane="Work",
        score=None,
        since=None,
        until=None,
        interiority=interiority,
    )


def _map_client(*, edges=None, headers=None, zone_fanout=8) -> TestClient:
    store = FakeNeighborStore(edges=edges, headers=headers)
    settings = Settings(graph_store_path="/tmp/store", map_zone_fanout=zone_fanout)
    graph = GraphService(settings=settings, store=store, nodes=FakeNodeReader(), capsule=None)
    app = FastAPI()
    app.include_router(search.router, prefix=PREFIX)
    app.dependency_overrides[get_graph_service] = lambda: graph
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_neighbors_grouped_returns_center_and_zones():
    edges = {_C1: [_nedge(_N1, rel="at"), _nedge(_N2, rel="involves", dir="in")]}
    headers = {
        _C1: NeighborHeader(node_id=_C1, type="person", title="Alex", plane="Work", planes=["Work"])
    }
    resp = _map_client(edges=edges, headers=headers).get(f"{PREFIX}/nodes/{_C1}/neighbors")
    assert resp.status_code == 200
    body = resp.json()
    assert body["center"]["node_id"] == _C1 and body["center"]["plane"] == "Work"
    assert [z["rel"] for z in body["zones"]] == ["at", "involves"]  # rel-keyed (ADR-052)
    assert "origin" not in body["zones"][0]  # no zone-level origin
    n = body["zones"][0]["neighbors"][0]
    assert n["node_id"] == _N1 and n["plane"] == "Work" and n["dir"] == "out"
    assert n["origin"] == "canonical"  # per-neighbor origin drives styling


def test_neighbors_expose_interiority_on_center_and_neighbors():
    # M8.2 T3.5: the map carries `interiority` on every neighbor AND the center (ADR-055 §3c) so the
    # canvas marks internal/mixed nodes without a second fetch; null on an unstamped hub.
    edges = {
        _C1: [
            _nedge(_N1, rel="at", interiority="internal"),
            _nedge(_N2, rel="involves", dir="in", interiority=None),
        ]
    }
    headers = {
        _C1: NeighborHeader(
            node_id=_C1,
            type="memory",
            title="Alex",
            plane="Work",
            planes=["Work"],
            interiority="mixed",
        )
    }
    body = _map_client(edges=edges, headers=headers).get(f"{PREFIX}/nodes/{_C1}/neighbors").json()
    assert body["center"]["interiority"] == "mixed"
    zones = {z["rel"]: z for z in body["zones"]}
    assert zones["at"]["neighbors"][0]["interiority"] == "internal"
    assert zones["involves"]["neighbors"][0]["interiority"] is None


def test_neighbors_zone_overflow_carries_total_and_cursor():
    edges = {_C1: [_nedge(f"{i:08d}-1111-1111-1111-111111111111") for i in range(4)]}
    headers = {_C1: NeighborHeader(node_id=_C1, type="person", title="A", plane=None, planes=[])}
    resp = _map_client(edges=edges, headers=headers, zone_fanout=2).get(
        f"{PREFIX}/nodes/{_C1}/neighbors"
    )
    zone = resp.json()["zones"][0]
    assert zone["total"] == 4 and len(zone["neighbors"]) == 2 and zone["next_cursor"]


def test_neighbors_show_more_mode_pages_single_zone():
    edges = {_C1: [_nedge(f"{i:08d}-1111-1111-1111-111111111111") for i in range(4)]}
    client = _map_client(edges=edges, zone_fanout=2)
    grouped = client.get(f"{PREFIX}/nodes/{_C1}/neighbors").json()
    cursor = grouped["zones"][0]["next_cursor"]
    resp = client.get(
        f"{PREFIX}/nodes/{_C1}/neighbors", params={"rel": "involves", "cursor": cursor}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["center_id"] == _C1 and body["rel"] == "involves"
    assert [n["node_id"] for n in body["neighbors"]] == [
        "00000002-1111-1111-1111-111111111111",
        "00000003-1111-1111-1111-111111111111",
    ]
    assert body["next_cursor"] is None


def test_neighbors_unknown_node_center_null_empty_zones():
    resp = _map_client(edges={}, headers={}).get(f"{PREFIX}/nodes/{_C1}/neighbors")
    assert resp.status_code == 200
    assert resp.json() == {"center": None, "zones": []}


def test_neighbors_bad_direction_is_422():
    resp = _map_client().get(f"{PREFIX}/nodes/{_C1}/neighbors", params={"direction": "sideways"})
    assert resp.status_code == 422


def test_neighbors_bad_cursor_is_422():
    resp = _map_client().get(
        f"{PREFIX}/nodes/{_C1}/neighbors", params={"rel": "involves", "cursor": "not-base64!!"}
    )
    assert resp.status_code == 422


def test_neighbors_malformed_uuid_is_422():
    resp = _map_client().get(f"{PREFIX}/nodes/not-a-uuid/neighbors")
    assert resp.status_code == 422


# --- token edit (PUT /nodes/{id}/date-token, M8.2 · ADR-056 §5) ---------------------------

_NODE = "11111111-1111-1111-1111-111111111111"


class FakeNodeTimeEditService:
    def __init__(self, *, result=None, raises=None) -> None:
        self.result = result
        self.raises = raises
        self.args = None

    async def edit_token(self, node_id, *, old_token, start, end=None, label=None):
        self.args = {
            "node_id": node_id,
            "old": old_token,
            "start": start,
            "end": end,
            "label": label,
        }
        if self.raises is not None:
            raise self.raises
        return self.result


def _edit_client(service) -> TestClient:
    from app.dependencies import get_node_time_edit_service

    app = FastAPI()
    app.include_router(search.router, prefix=PREFIX)
    app.dependency_overrides[get_node_time_edit_service] = lambda: service
    app.dependency_overrides[require_session] = lambda: None
    return TestClient(app)


def test_edit_date_token_returns_result():
    from app.services.node_time_edit import TimeEditResult

    svc = FakeNodeTimeEditService(
        result=TimeEditResult(
            node_id=_NODE, occurred_updated=True, occurred="2025-08", occurred_end=None
        )
    )
    resp = _edit_client(svc).put(
        f"{PREFIX}/nodes/{_NODE}/date-token",
        json={"old": "[[t:2025-07-07|7 July 2025]]", "start": "2025-08"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["occurred_updated"] is True and body["occurred"] == "2025-08"
    assert svc.args["old"] == "[[t:2025-07-07|7 July 2025]]" and svc.args["start"] == "2025-08"


def test_edit_date_token_unknown_node_is_404():
    from app.services.node_time_edit import NodeNotFound

    svc = FakeNodeTimeEditService(raises=NodeNotFound(_NODE))
    resp = _edit_client(svc).put(
        f"{PREFIX}/nodes/{_NODE}/date-token", json={"old": "[[t:2025]]", "start": "2026"}
    )
    assert resp.status_code == 404


def test_edit_date_token_bad_payload_is_400():
    from app.services.node_time_edit import BadTimeEdit

    svc = FakeNodeTimeEditService(raises=BadTimeEdit("bad date"))
    resp = _edit_client(svc).put(
        f"{PREFIX}/nodes/{_NODE}/date-token", json={"old": "[[t:2025]]", "start": "nope"}
    )
    assert resp.status_code == 400


def test_edit_date_token_malformed_uuid_is_422():
    svc = FakeNodeTimeEditService()
    resp = _edit_client(svc).put(
        f"{PREFIX}/nodes/not-a-uuid/date-token", json={"old": "[[t:2025]]", "start": "2026"}
    )
    assert resp.status_code == 422
