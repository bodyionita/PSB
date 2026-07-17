"""NodeWriter edge-materialization tests (ADR-030 §3, M3 task 4).

``append_edges`` is pure (frontmatter in → frontmatter out); ``add_edges`` is the atomic file
mutation the review service uses to draw a resolved entity edge onto an existing node file.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from app.graph.node_writer import (
    NodeDocument,
    NodeEdge,
    NodeWriter,
    append_edges,
    merged_alias_union,
    render_node,
    render_tombstone,
    replace_body_token,
    retarget_edges,
    retype_edge,
    set_occurred_frontmatter,
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


# --- retype_edge (ADR-036, M3 task 7b) ----------------------------------------------------------


def test_retype_edge_changes_only_the_matching_edge_rel():
    raw = render_node(
        _memory_doc(
            edges=(
                NodeEdge(rel="involves", to="person-1", conf=0.9, since="2026-07-12"),
                NodeEdge(rel="involves", to="person-2"),
                NodeEdge(rel="about", to="topic-9"),
            )
        )
    )
    out, count = retype_edge(raw, to="person-1", from_rel="involves", to_rel="mentors")
    assert count == 1
    meta = parse_node_metadata(out, store_path="memory/a.md", fallback_created=CREATED)
    # only the (involves, person-1) edge became mentors; conf/since preserved, others untouched.
    assert {(e.rel, e.to) for e in meta.edges} == {
        ("mentors", "person-1"),
        ("involves", "person-2"),
        ("about", "topic-9"),
    }
    mentors = next(e for e in meta.edges if e.rel == "mentors")
    assert mentors.conf == 0.9 and mentors.since == date(2026, 7, 12)


def test_retype_edge_drops_duplicate_after_retype():
    # The node already carries the target rel to the same node → the re-typed edge collapses on it.
    raw = render_node(
        _memory_doc(
            edges=(
                NodeEdge(rel="involves", to="person-1", since="2026-07-12"),
                NodeEdge(rel="mentors", to="person-1"),
            )
        )
    )
    out, count = retype_edge(raw, to="person-1", from_rel="involves", to_rel="mentors")
    assert count == 1
    meta = parse_node_metadata(out, store_path="memory/a.md", fallback_created=CREATED)
    assert [(e.rel, e.to) for e in meta.edges] == [("mentors", "person-1")]


def test_retype_edge_does_not_match_a_prefix_colliding_rel():
    # from_rel "at" must NOT corrupt an edge whose rel is "at_home" to the same target (field
    # boundary, not substring). The intended {rel: at, to: place-1} edge isn't present → no change.
    raw = render_node(_memory_doc(edges=(NodeEdge(rel="at_home", to="place-1"),)))
    out, count = retype_edge(raw, to="place-1", from_rel="at", to_rel="located_at")
    assert count == 0 and out == raw
    # and when both exist, only the exact-rel edge is re-typed.
    raw2 = render_node(
        _memory_doc(edges=(NodeEdge(rel="at", to="place-1"), NodeEdge(rel="at_home", to="place-1")))
    )
    out2, count2 = retype_edge(raw2, to="place-1", from_rel="at", to_rel="located_at")
    assert count2 == 1
    meta = parse_node_metadata(out2, store_path="memory/a.md", fallback_created=CREATED)
    rels = {(e.rel, e.to) for e in meta.edges}
    assert rels == {("located_at", "place-1"), ("at_home", "place-1")}


def test_retype_edge_no_match_is_verbatim():
    raw = render_node(_memory_doc(edges=(NodeEdge(rel="about", to="topic-9"),)))
    # wrong rel and wrong target both leave the file byte-identical.
    assert retype_edge(raw, to="topic-9", from_rel="involves", to_rel="mentors") == (raw, 0)
    assert retype_edge(raw, to="other", from_rel="about", to_rel="mentors") == (raw, 0)


def test_writer_retype_edge_atomic(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [src] = writer.write_nodes(
        [_memory_doc(edges=(NodeEdge(rel="involves", to="person-1", since="2026-07-12"),))]
    )
    count = writer.retype_edge(src.store_path, to="person-1", from_rel="involves", to_rel="mentors")
    assert count == 1
    text = (tmp_path / Path(*src.store_path.split("/"))).read_text(encoding="utf-8")
    meta = parse_node_metadata(text, store_path=src.store_path, fallback_created=CREATED)
    assert [(e.rel, e.to) for e in meta.edges] == [("mentors", "person-1")]


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


# --- Diacritic folding at the write chokepoint (ADR-041, M3 task 11) ---


def test_write_nodes_folds_every_derived_field(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [written] = writer.write_nodes(
        [
            NodeDocument(
                id="22222222-2222-4222-8222-222222222222",
                type="person",
                title="Mădălina Fairfax",
                body="Ștefan met Mădălina in Iași.",
                created_local=CREATED,
                source="text",
                tags=("prietenă",),
                aliases=("Mădă", "Mădălina"),
                disambig="prietenă din copilărie",
            )
        ]
    )
    # Filename slug is folded (no `m-d-lina` mangling).
    assert "madalina-fairfax" in written.store_path
    raw = (tmp_path / Path(*written.store_path.split("/"))).read_text(encoding="utf-8")
    # Nothing written to the store carries a diacritic — every derived field folded.
    assert "ă" not in raw and "ș" not in raw and "Ș" not in raw and "ț" not in raw
    meta = parse_node_metadata(raw, store_path=written.store_path, fallback_created=CREATED)
    assert meta.title == "Madalina Fairfax"
    assert meta.aliases == ["Mada", "Madalina"]
    assert meta.disambig == "prietena din copilarie"
    assert meta.tags == ["prietena"]
    assert "Stefan met Madalina in Iasi." in raw


def test_set_aliases_folds(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [hub] = writer.write_nodes(
        [
            NodeDocument(
                id="33333333-3333-4333-8333-333333333333",
                type="person",
                title="Horia",
                body="",
                created_local=CREATED,
                source="text",
                aliases=("Horia",),
            )
        ]
    )
    writer.set_aliases(hub.store_path, ["Horia", "Horia Ashford"])
    raw = (tmp_path / Path(*hub.store_path.split("/"))).read_text(encoding="utf-8")
    meta = parse_node_metadata(raw, store_path=hub.store_path, fallback_created=CREATED)
    assert meta.aliases == ["Horia", "Horia Ashford"]


# --- Type-aware removal + full reset (ADR-038 / ADR-042, M3 task 11) ---


def test_remove_nodes_keeps_entity_hub_types(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [mem] = writer.write_nodes([_memory_doc()])
    [hub] = writer.write_nodes(
        [
            NodeDocument(
                id="44444444-4444-4444-8444-444444444444",
                type="person",
                title="Alex",
                body="",
                created_local=CREATED,
                source="text",
                aliases=("Alex",),
            )
        ]
    )
    removed = writer.remove_nodes([mem.store_path, hub.store_path], keep_types={"person"})
    assert removed == [mem.store_path]  # only the content node removed
    assert not (tmp_path / Path(*mem.store_path.split("/"))).exists()
    assert (tmp_path / Path(*hub.store_path.split("/"))).exists()  # hub preserved (ADR-038)


def test_remove_all_nodes_clears_the_store(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    written = writer.write_nodes(
        [
            _memory_doc(),
            NodeDocument(
                id="55555555-5555-4555-8555-555555555555",
                type="person",
                title="Bob",
                body="",
                created_local=CREATED,
                source="text",
            ),
        ]
    )
    count = writer.remove_all_nodes(ignore={".git", "templates"})
    assert count == 2
    for w in written:
        assert not (tmp_path / Path(*w.store_path.split("/"))).exists()


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
    assert (
        parse_node_metadata(tomb, store_path=loser.store_path, fallback_created=CREATED).merged_into
        == "survivor-2"
    )
    assert not list((tmp_path / "person").glob(".*.tmp"))


# --- interiority + occurred_end round-trip (M8.2 · ADR-055/056) --------------------------


def test_content_node_renders_interiority_and_occurred_range_and_parses_back():
    doc = NodeDocument(
        id="22222222-2222-4222-8222-222222222222",
        type="memory",
        title="Summer ease",
        body="It felt easy [[t:2025-06/2025-08|summer 2025]].",
        created_local=CREATED,
        source="text",
        occurred="2025-06",
        occurred_end="2025-08",
        interiority="internal",
    )
    raw = render_node(doc)
    assert "interiority: internal" in raw
    meta = parse_node_metadata(raw, store_path="memory/x.md", fallback_created=CREATED)
    assert meta.interiority == "internal"
    # occurred "2025-06" + explicit occurred_end "2025-08" → the summer day-range.
    assert meta.occurred_start == date(2025, 6, 1)
    assert meta.occurred_end == date(2025, 8, 31)


def test_entity_hub_node_omits_interiority_line():
    # A hub carries interiority=None (the dimension is a property of content, not a thin hub).
    hub = NodeDocument(
        id="33333333-3333-4333-8333-333333333333",
        type="person",
        title="Alex",
        body="(profile is derived)",
        created_local=CREATED,
        source="text",
        aliases=("alex",),
    )
    raw = render_node(hub)
    assert "interiority:" not in raw
    assert (
        parse_node_metadata(raw, store_path="person/a.md", fallback_created=CREATED).interiority
        is None
    )


# --- Two-tier date edits: token replace + occurred setter (M8.2 · ADR-056 §5/§7) ----------


def _dated_doc() -> NodeDocument:
    return NodeDocument(
        id="66666666-6666-4666-8666-666666666666",
        type="memory",
        title="A trip",
        body="We left [[t:2025-07-07|7 July 2025]] and it was warm.",
        created_local=CREATED,
        source="text",
        occurred="2025-07-07",
    )


def test_replace_body_token_touches_body_only():
    raw = render_node(_dated_doc())
    out, replaced = replace_body_token(
        raw, "[[t:2025-07-07|7 July 2025]]", "[[t:2025-08|August 2025]]"
    )
    assert replaced == 1
    assert "[[t:2025-08|August 2025]]" in out
    assert "[[t:2025-07-07" not in out
    # Frontmatter untouched (occurred still the old value — the setter is a separate concern).
    assert "occurred: 2025-07-07" in out


def test_replace_body_token_not_found_is_verbatim():
    raw = render_node(_dated_doc())
    out, replaced = replace_body_token(raw, "[[t:1999-01-01]]", "[[t:2000]]")
    assert replaced == 0 and out == raw


def test_replace_body_token_no_frontmatter_is_noop():
    out, replaced = replace_body_token("plain text [[t:2025]]", "[[t:2025]]", "[[t:2026]]")
    assert replaced == 0 and out == "plain text [[t:2025]]"


def test_set_occurred_frontmatter_inserts_replaces_and_clears():
    # A memory with no occurred: inserting sets both lines in order, ahead of `source`.
    raw = render_node(
        NodeDocument(
            id="77777777-7777-4777-8777-777777777777",
            type="memory",
            title="Undated",
            body="something happened",
            created_local=CREATED,
            source="text",
        )
    )
    assert "occurred:" not in raw
    inserted = set_occurred_frontmatter(raw, occurred="2019", occurred_end=None)
    meta = parse_node_metadata(inserted, store_path="memory/u.md", fallback_created=CREATED)
    assert meta.occurred_start == date(2019, 1, 1) and meta.occurred_end == date(2019, 12, 31)
    # occurred sits before source (contract key order).
    assert inserted.index("occurred: 2019") < inserted.index("source:")
    # Replacing with a range writes both lines; occurred_end follows occurred.
    ranged = set_occurred_frontmatter(inserted, occurred="2025-06", occurred_end="2025-08")
    assert ranged.index("occurred: 2025-06") < ranged.index("occurred_end: 2025-08")
    rmeta = parse_node_metadata(ranged, store_path="memory/u.md", fallback_created=CREATED)
    assert rmeta.occurred_start == date(2025, 6, 1) and rmeta.occurred_end == date(2025, 8, 31)
    # Clearing removes both lines.
    cleared = set_occurred_frontmatter(ranged, occurred=None, occurred_end=None)
    assert "occurred:" not in cleared and "occurred_end:" not in cleared


def test_writer_set_occurred_atomic(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [written] = writer.write_nodes(
        [
            NodeDocument(
                id="88888888-8888-4888-8888-888888888888",
                type="memory",
                title="Undated",
                body="it happened",
                created_local=CREATED,
                source="text",
            )
        ]
    )
    assert writer.set_occurred(written.store_path, occurred="2019", occurred_end=None) is True
    raw = (tmp_path / Path(*written.store_path.split("/"))).read_text(encoding="utf-8")
    meta = parse_node_metadata(raw, store_path=written.store_path, fallback_created=CREATED)
    assert meta.occurred_start == date(2019, 1, 1) and meta.occurred_end == date(2019, 12, 31)
    assert not list((tmp_path / "memory").glob(".*.tmp"))


def test_writer_edit_time_token_updates_event_date(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [written] = writer.write_nodes([_dated_doc()])
    # Editing the event-date token to August: body token + occurred both move.
    replaced = writer.edit_time_token(
        written.store_path,
        old_token="[[t:2025-07-07|7 July 2025]]",
        new_token="[[t:2025-08|August 2025]]",
        occurred="2025-08",
        occurred_end=None,
        update_occurred=True,
    )
    assert replaced == 1
    raw = (tmp_path / Path(*written.store_path.split("/"))).read_text(encoding="utf-8")
    assert "[[t:2025-08|August 2025]]" in raw
    meta = parse_node_metadata(raw, store_path=written.store_path, fallback_created=CREATED)
    assert meta.occurred_start == date(2025, 8, 1) and meta.occurred_end == date(2025, 8, 31)


def test_writer_edit_time_token_missing_token_no_write(tmp_path: Path):
    writer = NodeWriter(str(tmp_path))
    [written] = writer.write_nodes([_dated_doc()])
    replaced = writer.edit_time_token(
        written.store_path,
        old_token="[[t:1999]]",
        new_token="[[t:2000]]",
        occurred="2000",
        occurred_end=None,
        update_occurred=True,
    )
    assert replaced == 0
    raw = (tmp_path / Path(*written.store_path.split("/"))).read_text(encoding="utf-8")
    assert "occurred: 2025-07-07" in raw  # occurred untouched when the token wasn't found
