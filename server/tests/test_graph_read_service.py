"""GraphService tests: fake neighbor store + fake node reader (no DB, no live LLM).

Covers the M5 task-1 read primitives — the cursor-paginated one-hop ``neighbors`` (rel/direction
filters, limit clamping, keyset round-trip, bad input) and the ``build_context`` bundle (depth
clamping, fanout truncation, cycle guard). The store fake replicates the real store's ordering +
keyset + filters so pagination is exercised for real, not stubbed.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.graph.service import (
    GraphService,
    InvalidCursor,
    InvalidDirection,
    _decode_cursor,
    _encode_cursor,
)
from app.graph.store import NeighborEdge, NeighborHeader
from app.identity.store import CapsuleBlob
from app.search.service import NodePreview

from .fakes import FakeCapsuleStore, FakeNeighborStore, FakeNodeReader


def _edge(
    node_id: str,
    *,
    origin: str = "canonical",
    rel: str = "involves",
    dir: str = "out",
    plane: str | None = "Work",
    score: float | None = None,
) -> NeighborEdge:
    return NeighborEdge(
        origin=origin,
        rel=rel,
        dir=dir,
        node_id=node_id,
        type="person",
        title=node_id.upper(),
        plane=plane,
        score=score,
        since=None,
        until=None,
    )


def _header(node_id: str = "c1", *, type: str = "person") -> NeighborHeader:
    return NeighborHeader(
        node_id=node_id, type=type, title=node_id.upper(), plane="Work", planes=["Work"]
    )


def _preview(node_id: str = "c1") -> NodePreview:
    return NodePreview(
        node_id=node_id,
        store_path=f"memory/{node_id}.md",
        type="memory",
        title=node_id.upper(),
        plane="Work",
        planes=["Work"],
        tags=[],
        aliases=[],
        disambig=None,
        occurred=None,
        occurred_end=None,
        body="body",
        profile=None,
        edges=[],
        merged_into=None,
    )


def _service(
    *,
    edges: dict[str, list[NeighborEdge]] | None = None,
    nodes: dict[str, NodePreview] | None = None,
    headers: dict[str, NeighborHeader] | None = None,
    page_default: int = 25,
    page_max: int = 100,
    depth_default: int = 1,
    depth_max: int = 2,
    fanout: int = 10,
    zone_fanout: int = 8,
    capsule=None,
) -> tuple[GraphService, FakeNeighborStore, FakeNodeReader]:
    store = FakeNeighborStore(edges=edges, headers=headers)
    reader = FakeNodeReader(nodes=nodes)
    settings = Settings(
        graph_store_path="/tmp/store",
        graph_neighbors_page_default=page_default,
        graph_neighbors_page_max=page_max,
        build_context_default_depth=depth_default,
        build_context_max_depth=depth_max,
        build_context_fanout=fanout,
        map_zone_fanout=zone_fanout,
    )
    service = GraphService(settings=settings, store=store, nodes=reader, capsule=capsule)
    return service, store, reader


# --- neighbors ------------------------------------------------------------------------------


async def test_neighbors_returns_ordered_page_no_cursor_when_exhausted():
    edges = {"c1": [_edge("p2", rel="at"), _edge("m1", rel="involves", dir="in")]}
    service, _, _ = _service(edges=edges)
    page = await service.neighbors("c1")
    assert [e.node_id for e in page.neighbors] == ["p2", "m1"]  # (origin,rel,dir,id) order
    assert page.next_cursor is None  # everything fit in one page
    assert page.center_id == "c1" and page.direction == "both" and page.rel is None


async def test_neighbors_both_direction_passes_none_to_store():
    service, store, _ = _service(edges={"c1": []})
    await service.neighbors("c1")
    assert store.calls[-1]["direction"] is None  # "both" → no direction filter


@pytest.mark.parametrize("direction", ["out", "in"])
async def test_neighbors_direction_filter_forwarded(direction: str):
    service, store, _ = _service(edges={"c1": []})
    await service.neighbors("c1", direction=direction)
    assert store.calls[-1]["direction"] == direction


async def test_neighbors_rejects_bad_direction():
    service, _, _ = _service(edges={"c1": []})
    with pytest.raises(InvalidDirection):
        await service.neighbors("c1", direction="sideways")


async def test_neighbors_rel_filter_forwarded_empty_is_no_filter():
    service, store, _ = _service(edges={"c1": []})
    await service.neighbors("c1", rel="involves")
    assert store.calls[-1]["rel"] == "involves"
    await service.neighbors("c1", rel="")
    assert store.calls[-1]["rel"] is None  # empty string means no filter


async def test_neighbors_clamps_limit():
    service, store, _ = _service(edges={"c1": []}, page_default=25, page_max=50)
    await service.neighbors("c1")
    assert store.calls[-1]["limit"] == 26  # default 25 + 1 (over-fetch to detect a next page)
    await service.neighbors("c1", limit=9999)
    assert store.calls[-1]["limit"] == 51  # clamped to page_max 50, + 1
    await service.neighbors("c1", limit=0)
    assert store.calls[-1]["limit"] == 2  # floored to 1, + 1


async def test_neighbors_paginates_via_cursor():
    edges = {
        "c1": [
            _edge("p2", rel="at"),
            _edge("m1", rel="involves", dir="in"),
            _edge("x9", origin="derived", rel="similar"),
        ]
    }
    service, _, _ = _service(edges=edges)
    first = await service.neighbors("c1", limit=2)
    assert [e.node_id for e in first.neighbors] == ["p2", "m1"]
    assert first.next_cursor is not None  # a third neighbor remains

    second = await service.neighbors("c1", limit=2, cursor=first.next_cursor)
    assert [e.node_id for e in second.neighbors] == ["x9"]
    assert second.next_cursor is None  # exhausted


async def test_neighbors_cursor_encodes_last_returned_keyset():
    edge = _edge("m1", origin="canonical", rel="involves", dir="in")
    assert _decode_cursor(_encode_cursor(edge)) == ("canonical", "involves", "in", "m1")


async def test_neighbors_rejects_malformed_cursor():
    service, _, _ = _service(edges={"c1": []})
    with pytest.raises(InvalidCursor):
        await service.neighbors("c1", cursor="not-base64!!")


async def test_neighbors_unknown_node_is_empty_page():
    service, _, _ = _service(edges={})  # no edges for anyone
    page = await service.neighbors("ghost")
    assert page.neighbors == [] and page.next_cursor is None


# --- neighbor_zones (M7 map grouped mode, ADR-051 §2) ---------------------------------------


async def test_neighbor_zones_groups_by_rel_with_center():
    edges = {
        "c1": [
            _edge("p2", rel="at"),
            _edge("m1", rel="involves", dir="in"),
            _edge("m3", rel="involves", dir="out"),
            _edge("x9", origin="derived", rel="similar"),
        ]
    }
    service, _, _ = _service(edges=edges, headers={"c1": _header("c1")})
    result = await service.neighbor_zones("c1")
    assert result.center is not None and result.center.node_id == "c1"
    # Zones are one per rel, ordered by rel (ADR-052) — no zone-level origin.
    assert [z.rel for z in result.zones] == ["at", "involves", "similar"]
    involves = result.zones[1]
    assert [e.node_id for e in involves.neighbors] == ["m1", "m3"]  # (origin,dir,id): in<out
    assert involves.total == 2 and involves.next_cursor is None


async def test_neighbor_zones_merges_dual_origin_similar_into_one_zone():
    # ADR-052 / review Finding 1: canonical `similar` (a human link) + derived `similar` (recompute)
    # collapse into ONE zone, canonical ordered first; each neighbor keeps its own origin.
    edges = {
        "c1": [
            _edge("d1", origin="derived", rel="similar"),
            _edge("k1", origin="canonical", rel="similar"),
        ]
    }
    service, _, _ = _service(edges=edges, headers={"c1": _header("c1")})
    result = await service.neighbor_zones("c1")
    assert [z.rel for z in result.zones] == ["similar"]  # one zone, not two
    zone = result.zones[0]
    pairs = [(e.origin, e.node_id) for e in zone.neighbors]
    assert pairs == [("canonical", "k1"), ("derived", "d1")]
    assert zone.total == 2 and zone.next_cursor is None


async def test_neighbor_zones_dual_origin_similar_cursor_resumes_across_origins():
    # The Finding-1 fix in action: fanout caps the first page to canonical-similar; the rel-only
    # "show more" resumes strictly into derived-similar with no bleed and no duplicate.
    edges = {
        "c1": [
            _edge("k1", origin="canonical", rel="similar"),
            _edge("k2", origin="canonical", rel="similar"),
            _edge("d1", origin="derived", rel="similar"),
        ]
    }
    service, _, _ = _service(edges=edges, headers={"c1": _header("c1")}, zone_fanout=2)
    zone = (await service.neighbor_zones("c1")).zones[0]
    assert [e.node_id for e in zone.neighbors] == ["k1", "k2"]  # canonical first, capped at 2
    assert zone.total == 3 and zone.next_cursor is not None
    page = await service.neighbors("c1", rel="similar", cursor=zone.next_cursor)
    assert [e.node_id for e in page.neighbors] == ["d1"]  # resumes into derived, no dup/bleed
    assert page.next_cursor is None


async def test_neighbor_zones_caps_each_zone_with_total_and_cursor():
    many = [_edge(f"n{i:02d}", rel="involves") for i in range(5)]
    service, _, _ = _service(edges={"c1": many}, headers={"c1": _header("c1")}, zone_fanout=3)
    result = await service.neighbor_zones("c1")
    zone = result.zones[0]
    assert len(zone.neighbors) == 3  # capped to zone_fanout
    assert zone.total == 5  # full zone size for "show 2 more of 5"
    assert zone.next_cursor is not None  # more remain → paging token


async def test_neighbor_zones_cursor_resumes_the_zone_via_neighbors():
    # The zone's next_cursor must feed the single-zone "show more" (service.neighbors rel-filtered).
    many = [_edge(f"n{i:02d}", rel="involves") for i in range(5)]
    service, _, _ = _service(edges={"c1": many}, headers={"c1": _header("c1")}, zone_fanout=3)
    zone = (await service.neighbor_zones("c1")).zones[0]
    page = await service.neighbors("c1", rel="involves", cursor=zone.next_cursor)
    assert [e.node_id for e in page.neighbors] == ["n03", "n04"]  # strictly past the first 3
    assert page.next_cursor is None


async def test_neighbor_zones_direction_scopes_zones_and_totals():
    edges = {
        "c1": [
            _edge("m1", rel="involves", dir="in"),
            _edge("m2", rel="involves", dir="in"),
            _edge("p3", rel="involves", dir="out"),
        ]
    }
    service, store, _ = _service(edges=edges, headers={"c1": _header("c1")})
    result = await service.neighbor_zones("c1", direction="in")
    assert store.calls[-1]["direction"] == "in"  # "both"→None, else forwarded
    zone = result.zones[0]
    assert [e.node_id for e in zone.neighbors] == ["m1", "m2"]
    assert zone.total == 2  # the out edge is not counted under direction=in


async def test_neighbor_zones_both_direction_passes_none_to_store():
    service, store, _ = _service(edges={"c1": []}, headers={"c1": _header("c1")})
    await service.neighbor_zones("c1")
    assert store.calls[-1]["direction"] is None


async def test_neighbor_zones_unknown_node_center_none_empty_zones():
    service, _, _ = _service(edges={}, headers={})
    result = await service.neighbor_zones("ghost")
    assert result.center is None and result.zones == []


async def test_neighbor_zones_rejects_bad_direction():
    service, _, _ = _service(edges={"c1": []})
    with pytest.raises(InvalidDirection):
        await service.neighbor_zones("c1", direction="sideways")


# --- build_context --------------------------------------------------------------------------


async def test_build_context_unknown_node_returns_none():
    service, _, _ = _service(nodes={})
    assert await service.build_context("missing") is None


async def test_build_context_depth_zero_is_node_only():
    service, store, _ = _service(nodes={"c1": _preview()}, edges={"c1": [_edge("p2")]})
    ctx = await service.build_context("c1", depth=0)
    assert ctx is not None
    assert ctx.node.node_id == "c1"
    assert ctx.neighbors == [] and ctx.depth == 0 and ctx.truncated is False
    assert ctx.identity_capsule is None  # no capsule reader wired → L0 omitted
    assert store.calls == []  # depth 0 never touches the neighbor store


async def test_build_context_depth_one_lists_neighbors_without_children():
    edges = {"c1": [_edge("p2", rel="at"), _edge("m1", rel="involves", dir="in")]}
    service, _, _ = _service(nodes={"c1": _preview()}, edges=edges, depth_default=1)
    ctx = await service.build_context("c1")
    assert ctx is not None and ctx.depth == 1
    assert [n.edge.node_id for n in ctx.neighbors] == ["p2", "m1"]
    assert all(n.neighbors == [] and n.truncated is False for n in ctx.neighbors)


async def test_build_context_depth_two_expands_children():
    edges = {
        "c1": [_edge("p2", rel="at")],
        "p2": [_edge("m1", rel="involves", dir="in")],
    }
    service, _, _ = _service(nodes={"c1": _preview()}, edges=edges)
    ctx = await service.build_context("c1", depth=2)
    assert ctx is not None and ctx.depth == 2
    assert [n.edge.node_id for n in ctx.neighbors] == ["p2"]
    assert [c.edge.node_id for c in ctx.neighbors[0].neighbors] == ["m1"]


async def test_build_context_clamps_depth_to_max():
    edges = {
        "c1": [_edge("p2")],
        "p2": [_edge("m1")],
        "m1": [_edge("z9")],
    }
    service, _, _ = _service(nodes={"c1": _preview()}, edges=edges, depth_max=2)
    ctx = await service.build_context("c1", depth=99)
    assert ctx is not None and ctx.depth == 2  # hard-bounded at 2 (ADR-032)
    # two levels deep, no third: m1's neighbor z9 is never expanded into.
    assert ctx.neighbors[0].neighbors[0].edge.node_id == "m1"
    assert ctx.neighbors[0].neighbors[0].neighbors == []


async def test_build_context_fanout_truncates_and_flags():
    many = [_edge(f"n{i:02d}", rel=f"r{i:02d}") for i in range(5)]
    service, _, _ = _service(nodes={"c1": _preview()}, edges={"c1": many}, fanout=3)
    ctx = await service.build_context("c1", depth=1)
    assert ctx is not None
    assert len(ctx.neighbors) == 3  # capped to fanout
    assert ctx.truncated is True  # the rest are reachable via traverse


async def test_build_context_cycle_guard_does_not_reexpand():
    # A ↔ B: at depth 2, B is expanded once (its neighbor A is listed but not expanded again),
    # so traversal terminates instead of ping-ponging.
    edges = {
        "c1": [_edge("b", rel="involves")],
        "b": [_edge("c1", rel="involves", dir="in")],
    }
    service, _, _ = _service(nodes={"c1": _preview()}, edges=edges)
    ctx = await service.build_context("c1", depth=2)
    assert ctx is not None
    b = ctx.neighbors[0]
    assert b.edge.node_id == "b"
    back_to_a = b.neighbors[0]
    assert back_to_a.edge.node_id == "c1"  # A is listed under B...
    assert back_to_a.neighbors == []  # ...but not expanded again (already on the path)


# --- build_context L0 identity capsule (M5 task 2, ADR-046 §5) ------------------------------------


async def test_build_context_serves_identity_capsule_as_l0():

    capsule = FakeCapsuleStore(blob=CapsuleBlob(text="The user builds a second brain."))
    service, _, _ = _service(nodes={"c1": _preview()}, edges={"c1": [_edge("p2")]}, capsule=capsule)
    # Present at depth 0 (node + capsule only) and when the tree is expanded.
    ctx0 = await service.build_context("c1", depth=0)
    assert ctx0 is not None and ctx0.identity_capsule == "The user builds a second brain."
    ctx1 = await service.build_context("c1", depth=1)
    assert ctx1 is not None and ctx1.identity_capsule == "The user builds a second brain."


async def test_build_context_omits_capsule_when_absent():

    capsule = FakeCapsuleStore(blob=None)  # no capsule generated yet
    service, _, _ = _service(nodes={"c1": _preview()}, capsule=capsule)
    ctx = await service.build_context("c1", depth=0)
    assert ctx is not None and ctx.identity_capsule is None


async def test_build_context_survives_a_failing_capsule_read():

    capsule = FakeCapsuleStore(raise_on_read=True)  # read boom — must not fail the bundle (rule 7)
    service, _, _ = _service(nodes={"c1": _preview()}, edges={"c1": [_edge("p2")]}, capsule=capsule)
    ctx = await service.build_context("c1", depth=1)
    assert ctx is not None
    assert ctx.identity_capsule is None  # omitted, best-effort
    assert [n.edge.node_id for n in ctx.neighbors] == ["p2"]  # the rest of the bundle intact
