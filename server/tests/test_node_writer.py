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
    merged_alias_union,
    render_node,
    render_tombstone,
    retarget_edges,
    upsert_frontmatter_list,
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


# --- Merge helpers (ADR-030 §5, M3 task 6) ---


def test_retarget_edges_redirects_to_survivor():
    raw = render_node(
        _memory_doc(
            edges=(
                NodeEdge(rel="involves", to="loser-1", since="2026-07-12"),
                NodeEdge(rel="about", to="topic-9"),
            )
        )
    )
    out, count = retarget_edges(raw, old_to="loser-1", new_to="survivor-2")
    assert count == 1
    meta = parse_node_metadata(out, store_path="memory/a.md", fallback_created=CREATED)
    assert {(e.rel, e.to) for e in meta.edges} == {("involves", "survivor-2"), ("about", "topic-9")}


def test_retarget_edges_drops_duplicate_after_redirect():
    # A node that already links the survivor with the same rel must not end with a duplicate edge.
    raw = render_node(
        _memory_doc(
            edges=(
                NodeEdge(rel="involves", to="loser-1", since="2026-07-12"),
                NodeEdge(rel="involves", to="survivor-2", since="2026-07-12"),
            )
        )
    )
    out, count = retarget_edges(raw, old_to="loser-1", new_to="survivor-2")
    assert count == 1
    meta = parse_node_metadata(out, store_path="memory/a.md", fallback_created=CREATED)
    assert [(e.rel, e.to) for e in meta.edges] == [("involves", "survivor-2")]


def test_retarget_edges_no_match_is_verbatim():
    raw = render_node(_memory_doc(edges=(NodeEdge(rel="about", to="topic-9"),)))
    out, count = retarget_edges(raw, old_to="loser-1", new_to="survivor-2")
    assert count == 0
    assert out == raw


def test_upsert_frontmatter_list_replaces_and_inserts():
    entity = NodeDocument(
        id="22222222-2222-4222-8222-222222222222",
        type="person",
        title="Alex",
        body="",
        created_local=CREATED,
        source="text",
        aliases=("alex",),
    )
    raw = render_node(entity)
    replaced = upsert_frontmatter_list(raw, "aliases", ["alex", "alexandru"])
    meta = parse_node_metadata(replaced, store_path="person/alex.md", fallback_created=CREATED)
    assert meta.aliases == ["alex", "alexandru"]

    # Insert when absent (a memory node has no aliases line).
    raw2 = render_node(_memory_doc(edges=(NodeEdge(rel="about", to="t"),)))
    inserted = upsert_frontmatter_list(raw2, "aliases", ["x"])
    meta2 = parse_node_metadata(inserted, store_path="memory/a.md", fallback_created=CREATED)
    assert meta2.aliases == ["x"]
    # The edges block still parses (the aliases line went in before it).
    assert [(e.rel, e.to) for e in meta2.edges] == [("about", "t")]


def test_render_tombstone_keeps_id_type_and_merged_into():
    text = render_tombstone(node_id="loser-1", node_type="person", survivor_id="survivor-2")
    meta = parse_node_metadata(text, store_path="person/loser.md", fallback_created=CREATED)
    assert meta.id == "loser-1"
    assert meta.type == "person"
    assert meta.merged_into == "survivor-2"


def test_merged_alias_union_dedupes_and_keeps_loser_forms():
    union = merged_alias_union(("alex",), "Alex", ("alexandru", "al"), "Alexandru Popescu")
    assert union == ["alex", "Alex", "Alexandru Popescu", "alexandru", "al"]


def test_writer_merge_methods_atomic(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [loser] = writer.write_nodes(
        [
            NodeDocument(
                id="loser-1",
                type="person",
                title="Alex",
                body="",
                created_local=CREATED,
                source="text",
                aliases=("alex",),
            )
        ]
    )
    [src] = writer.write_nodes(
        [_memory_doc(edges=(NodeEdge(rel="involves", to="loser-1", since="2026-07-12"),))]
    )
    assert writer.retarget_edges(src.store_path, old_to="loser-1", new_to="survivor-2") == 1
    writer.write_tombstone(
        loser.store_path, node_id="loser-1", node_type="person", survivor_id="survivor-2"
    )
    tomb = (tmp_path / Path(*loser.store_path.split("/"))).read_text(encoding="utf-8")
    assert parse_node_metadata(
        tomb, store_path=loser.store_path, fallback_created=CREATED
    ).merged_into == "survivor-2"
    assert not list((tmp_path / "person").glob(".*.tmp"))
