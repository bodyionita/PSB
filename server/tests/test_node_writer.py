"""NodeWriter edge-materialization tests (ADR-030 §3, M3 task 4).

``append_edges`` is pure (frontmatter in → frontmatter out); ``add_edges`` is the atomic file
mutation the review service uses to draw a resolved entity edge onto an existing node file.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.graph.node_writer import (
    NodeDocument,
    NodeEdge,
    NodeWriter,
    append_edges,
    render_node,
)
from app.indexing.frontmatter import parse_node_metadata

CREATED = datetime(2026, 7, 12, 12, 0, 0)


def _memory_doc(edges: tuple[NodeEdge, ...] = ()) -> NodeDocument:
    return NodeDocument(
        id="11111111-1111-4111-8111-111111111111",
        type="memory",
        title="A day out",
        body="We went to the park.",
        created_local=CREATED,
        source="text",
        edges=edges,
    )


def test_append_edges_creates_block_when_absent():
    raw = render_node(_memory_doc())
    assert "edges:" not in raw

    out = append_edges(raw, [NodeEdge(rel="involves", to="dst-1", since="2026-07-12")])

    meta = parse_node_metadata(out, store_path="memory/a.md", fallback_created=CREATED)
    assert [(e.rel, e.to) for e in meta.edges] == [("involves", "dst-1")]
    # Body is untouched.
    assert "We went to the park." in out


def test_append_edges_appends_to_existing_block():
    raw = render_node(_memory_doc(edges=(NodeEdge(rel="about", to="topic-9", since="2026-07-12"),)))
    out = append_edges(raw, [NodeEdge(rel="involves", to="person-2", since="2026-07-12")])

    meta = parse_node_metadata(out, store_path="memory/a.md", fallback_created=CREATED)
    assert {(e.rel, e.to) for e in meta.edges} == {("about", "topic-9"), ("involves", "person-2")}


def test_append_edges_is_idempotent_on_duplicate():
    raw = render_node(_memory_doc())
    once = append_edges(raw, [NodeEdge(rel="involves", to="dst-1", since="2026-07-12")])
    # A second append of the same rel+to is a no-op (dedup ignores since/until).
    twice = append_edges(once, [NodeEdge(rel="involves", to="dst-1", since="2030-01-01")])
    assert twice == once


def test_append_edges_no_frontmatter_raises():
    try:
        append_edges("no frontmatter here", [NodeEdge(rel="involves", to="x")])
    except ValueError:
        return
    raise AssertionError("expected ValueError for a file with no frontmatter")


def test_add_edges_writes_file_atomically(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [written] = writer.write_nodes([_memory_doc()])
    writer.add_edges(written.store_path, [NodeEdge(rel="involves", to="dst-1", since="2026-07-12")])

    raw = (tmp_path / Path(*written.store_path.split("/"))).read_text(encoding="utf-8")
    meta = parse_node_metadata(raw, store_path=written.store_path, fallback_created=CREATED)
    assert [(e.rel, e.to) for e in meta.edges] == [("involves", "dst-1")]
    # No stray temp files left behind.
    assert not list((tmp_path / "memory").glob(".*.tmp"))
