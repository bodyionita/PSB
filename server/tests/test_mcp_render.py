"""MCP Markdown renderer tests (M5 task 4, ADR-046 §3) — pure functions, no I/O.

Assert the token-efficient shape + the invariants that matter for an LLM: ids rendered verbatim
and labeled, the hub edge cap with a `traverse` overflow pointer, cursor pointers, and the
build_context identity-capsule L0.
"""

from __future__ import annotations

from app.graph.service import ContextNeighbor, NeighborPage, NodeContext
from app.graph.store import NeighborEdge
from app.mcp.render import (
    render_build_context,
    render_capture_ack,
    render_identity_capsule,
    render_node,
    render_planes,
    render_search_results,
    render_traverse,
    render_types,
)
from app.search.service import NodePreview
from app.search.store import NodeEdgeView, SearchHit

NID = "11111111-1111-1111-1111-111111111111"
NID2 = "22222222-2222-2222-2222-222222222222"


def _hit(**kw) -> SearchHit:
    base = dict(
        node_id=NID,
        store_path="p",
        type="memory",
        title="Pricing call",
        plane="Professional",
        planes=["Professional"],
        tags=["pricing"],
        snippet="We raised prices in Q2.",
        score=0.0321,
    )
    base.update(kw)
    return SearchHit(**base)


def _edge(node_id=NID2, rel="involves", direction="out", **kw) -> NodeEdgeView:
    base = dict(
        rel=rel,
        dir=direction,
        node_id=node_id,
        type="person",
        title="Alex",
        origin="canonical",
        score=None,
        since=None,
        until=None,
    )
    base.update(kw)
    return NodeEdgeView(**base)


def _neighbor(node_id=NID2, rel="involves", direction="out", **kw) -> NeighborEdge:
    base = dict(
        origin="canonical",
        rel=rel,
        dir=direction,
        node_id=node_id,
        type="person",
        title="Alex",
        plane="Professional",
        score=None,
        since=None,
        until=None,
    )
    base.update(kw)
    return NeighborEdge(**base)


def _node(**kw) -> NodePreview:
    base = dict(
        node_id=NID,
        store_path="p",
        type="memory",
        title="Pricing call",
        plane="Professional",
        planes=["Professional"],
        tags=["pricing"],
        aliases=[],
        disambig=None,
        occurred=None,
        occurred_end=None,
        body="We raised prices.",
        profile=None,
        edges=[],
        merged_into=None,
    )
    base.update(kw)
    return NodePreview(**base)


def test_search_results_render_ids_and_scores():
    md = render_search_results("pricing", [_hit()])
    assert "`" + NID + "`" in md  # id rendered verbatim, labeled
    assert "Pricing call" in md and "score 0.032" in md
    assert "memory, Professional" in md
    assert "build_context" in md  # nudges the next call


def test_search_results_empty():
    md = render_search_results("unicorns", [])
    assert "No nodes found" in md and "unicorns" in md


def test_node_renders_edges_and_caps():
    edges = [_edge(node_id=f"{i:08d}-0000-0000-0000-000000000000") for i in range(25)]
    md = render_node(_node(edges=edges), edge_cap=20)
    assert md.count("→ `involves`") == 20  # capped at 20
    assert "…5 more edge(s)" in md and 'traverse(id="' + NID + '")' in md
    assert "id: `" + NID + "`" in md


def test_node_merged_redirect():
    md = render_node(_node(merged_into=NID2), edge_cap=20)
    assert "merged into" in md and NID2 in md


def test_node_includes_profile_when_present():
    md = render_node(_node(profile="Alex is the CFO."), edge_cap=20)
    assert "## Profile" in md and "Alex is the CFO." in md


def test_traverse_render_with_cursor():
    page = NeighborPage(
        center_id=NID, neighbors=[_neighbor()], next_cursor="CUR", rel="involves", direction="both"
    )
    md = render_traverse(page)
    assert "`involves`" in md and NID2 in md
    assert 'cursor="CUR"' in md  # pagination pointer


def test_traverse_empty():
    page = NeighborPage(center_id=NID, neighbors=[], next_cursor=None, rel=None, direction="both")
    assert "No matching neighbors" in render_traverse(page)


def test_build_context_includes_capsule_and_tree():
    tree = [ContextNeighbor(edge=_neighbor(), neighbors=[], truncated=True)]
    ctx = NodeContext(
        node=_node(),
        neighbors=tree,
        depth=1,
        truncated=False,
        identity_capsule="The user runs a startup.",
    )
    md = render_build_context(ctx, edge_cap=20)
    assert "identity capsule" in md and "The user runs a startup." in md
    assert "## Context (depth 1)" in md
    assert "`involves`" in md and NID2 in md
    assert 'use `traverse(id="' + NID2 + '")' in md  # per-branch truncation pointer


def test_build_context_no_capsule():
    ctx = NodeContext(node=_node(), neighbors=[], depth=0, truncated=False, identity_capsule=None)
    md = render_build_context(ctx, edge_cap=20)
    assert "identity capsule" not in md
    assert "Pricing call" in md


def test_planes_and_types():
    assert "Professional" in render_planes(["Professional", "Health"], "inbox")
    md = render_types(["memory", "person"], ["involves"], ["person"])
    assert "node types" in md and "involves" in md and "person" in md


def test_capture_ack_and_identity():
    ack = render_capture_ack("cap-123")
    assert "`cap-123`" in ack and "search" in ack
    assert "About the user" in render_identity_capsule("I am a builder.")
    assert "No identity capsule" in render_identity_capsule(None)
