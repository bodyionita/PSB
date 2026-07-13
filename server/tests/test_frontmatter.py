"""Frontmatter parsing + node metadata extraction (pure, no I/O)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.indexing.frontmatter import parse_edges, parse_frontmatter, parse_node_metadata

FALLBACK = datetime(2020, 1, 1, tzinfo=UTC)

_PIPELINE_NODE = """---
id: 018f3c2e-abcd
type: memory
created: 2026-07-12T09:14:03+02:00
occurred: 2025-07
source: voice
source_ref: "slack:C0123/p1720771234.5678"
plane: Professional
planes: [Professional, Friends]
tags: [standup, planning]
organizer_version: v3
edges:
  - {rel: involves, to: 018f4a11-ffff, since: 2025-07-10}
  - {rel: about, to: 018f6c33-eeee}
---

# Weekly standup prep

Body text here.
"""


def test_parse_frontmatter_scalars_and_lists():
    fields = parse_frontmatter("source: voice\ntype: memory\ntags: [standup, planning]\n")
    assert fields["source"] == "voice"
    assert fields["type"] == "memory"
    assert fields["tags"] == ["standup", "planning"]


def test_parse_frontmatter_unquotes_scalars():
    fields = parse_frontmatter('source_ref: "slack:C0123/p123.456"\n')
    assert fields["source_ref"] == "slack:C0123/p123.456"


def test_parse_frontmatter_quoted_list_item_with_comma():
    fields = parse_frontmatter('aliases: ["my brother, Alex", "Alexandru"]\n')
    assert fields["aliases"] == ["my brother, Alex", "Alexandru"]


def test_parse_frontmatter_ignores_comments_and_blank_lines():
    fields = parse_frontmatter("# a comment\n\ntype: idea\n")
    assert fields == {"type": "idea"}


def test_parse_edges_reads_the_block_list():
    inner = (
        "edges:\n"
        "  - {rel: involves, to: 018f4a11-ffff, conf: 0.9, since: 2025-07-10}\n"
        "  - {rel: at, to: 018f7d44-aaaa, until: 2025-08-02}\n"
    )
    edges = parse_edges(inner)
    assert [(e.rel, e.to) for e in edges] == [
        ("involves", "018f4a11-ffff"),
        ("at", "018f7d44-aaaa"),
    ]
    assert edges[0].conf == 0.9 and edges[0].since == date(2025, 7, 10)
    assert edges[1].until == date(2025, 8, 2)


def test_parse_edges_skips_malformed_items():
    inner = "edges:\n  - {rel: involves}\n  - {to: x}\n  - {rel: about, to: n2}\n"
    edges = parse_edges(inner)
    assert [(e.rel, e.to) for e in edges] == [("about", "n2")]


def test_metadata_from_pipeline_node():
    meta = parse_node_metadata(
        _PIPELINE_NODE,
        store_path="memory/2026-07-12--standup--018f3c2e.md",
        fallback_created=FALLBACK,
    )
    assert meta.id == "018f3c2e-abcd"  # frontmatter id is the identity
    assert meta.type == "memory"
    assert meta.title == "Weekly standup prep"  # H1 wins over the filename stem
    assert meta.plane == "Professional"
    assert meta.planes == ["Professional", "Friends"]
    assert meta.tags == ["standup", "planning"]
    assert meta.source_ref == "slack:C0123/p1720771234.5678"
    assert meta.organizer_version == "v3"
    # occurred "2025-07" expands to the month range.
    assert meta.occurred_start == date(2025, 7, 1) and meta.occurred_end == date(2025, 7, 31)
    assert [(e.rel, e.to) for e in meta.edges] == [
        ("involves", "018f4a11-ffff"),
        ("about", "018f6c33-eeee"),
    ]


def test_metadata_type_falls_back_to_folder_then_memory():
    # A hand-authored node with no `type` takes the folder; a rootless file defaults to memory.
    node = "# A person\n\nno frontmatter"
    meta = parse_node_metadata(node, store_path="person/alex.md", fallback_created=FALLBACK)
    assert meta.type == "person"
    assert meta.plane is None  # plane does NOT fall back to the folder (folder = type now)
    root = parse_node_metadata("# Loose", store_path="loose.md", fallback_created=FALLBACK)
    assert root.type == "memory"


def test_metadata_id_falls_back_to_deterministic_uuid5():
    # A node file without an `id` gets a path-stable id (same across reindexes, no file write).
    node = "# X\n\nbody"
    a = parse_node_metadata(node, store_path="memory/x.md", fallback_created=FALLBACK)
    b = parse_node_metadata(node, store_path="memory/x.md", fallback_created=FALLBACK)
    assert a.id == b.id and len(a.id) == 36  # a uuid


def test_metadata_title_falls_back_to_stem_and_created_to_mtime():
    node = "Just a body, no heading, no frontmatter."
    meta = parse_node_metadata(
        node, store_path="memory/loose thought.md", fallback_created=FALLBACK
    )
    assert meta.title == "loose thought"
    assert meta.created == FALLBACK


def test_metadata_ignores_h1_inside_fenced_code_block():
    node = "---\ntype: idea\n---\n\n```\n# not a title\n```\n\n# Real title\n"
    meta = parse_node_metadata(node, store_path="idea/x.md", fallback_created=FALLBACK)
    assert meta.title == "Real title"


def test_metadata_merged_into_tombstone():
    node = "---\nid: loser-1\ntype: person\nmerged_into: survivor-9\n---\n# Alex\n"
    meta = parse_node_metadata(node, store_path="person/alex.md", fallback_created=FALLBACK)
    assert meta.merged_into == "survivor-9"
