"""Frontmatter parsing + note metadata extraction (pure, no I/O)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.indexing.frontmatter import parse_frontmatter, parse_note_metadata

FALLBACK = datetime(2020, 1, 1, tzinfo=UTC)

_PIPELINE_NOTE = """---
id: 018f3c2e-abcd
created: 2026-07-12T09:14:03+02:00
source: voice
source_ref: "slack:C0123/p1720771234.5678"
plane: Professional
planes: [Professional, Friends]
tags: [standup, planning]
related: ["Friends/2026-07-12 Dinner, plans with Alex.md"]
---

# Weekly standup prep

Body text here.
"""


def test_parse_frontmatter_scalars_and_lists():
    fields = parse_frontmatter(
        'source: voice\nplane: Professional\ntags: [standup, planning]\n'
    )
    assert fields["source"] == "voice"
    assert fields["plane"] == "Professional"
    assert fields["tags"] == ["standup", "planning"]


def test_parse_frontmatter_unquotes_scalars():
    fields = parse_frontmatter('source_ref: "slack:C0123/p123.456"\n')
    # A colon inside a quoted value must not split the value.
    assert fields["source_ref"] == "slack:C0123/p123.456"


def test_parse_frontmatter_quoted_list_item_with_comma():
    fields = parse_frontmatter('related: ["Friends/2026-07-12 Dinner, plans.md", "Ideas/x.md"]\n')
    assert fields["related"] == ["Friends/2026-07-12 Dinner, plans.md", "Ideas/x.md"]


def test_parse_frontmatter_ignores_comments_and_blank_lines():
    fields = parse_frontmatter("# a comment\n\nplane: Ideas\n")
    assert fields == {"plane": "Ideas"}


def test_metadata_from_pipeline_note():
    meta = parse_note_metadata(
        _PIPELINE_NOTE, vault_path="Professional/2026-07-12 Standup.md", fallback_created=FALLBACK
    )
    assert meta.title == "Weekly standup prep"  # H1 wins over the filename stem
    assert meta.plane == "Professional"
    assert meta.planes == ["Professional", "Friends"]
    assert meta.tags == ["standup", "planning"]
    assert meta.source == "voice"
    assert meta.source_ref == "slack:C0123/p1720771234.5678"
    assert meta.created == datetime.fromisoformat("2026-07-12T09:14:03+02:00")


def test_metadata_plane_falls_back_to_folder():
    note = "# A user note\n\nno frontmatter here"
    meta = parse_note_metadata(
        note, vault_path="Health/2026-07-12 Run.md", fallback_created=FALLBACK
    )
    assert meta.plane == "Health"  # top-level folder
    assert meta.planes == ["Health"]  # falls back to [plane]
    assert meta.tags == []
    assert meta.title == "A user note"


def test_metadata_title_falls_back_to_stem_and_created_to_mtime():
    note = "Just a body, no heading, no frontmatter."
    meta = parse_note_metadata(
        note, vault_path="Ideas/loose thought.md", fallback_created=FALLBACK
    )
    assert meta.title == "loose thought"  # filename stem when there's no H1
    assert meta.created == FALLBACK  # file mtime fallback when `created` is absent


def test_metadata_ignores_h1_inside_fenced_code_block():
    note = "---\nplane: Ideas\n---\n\n```\n# not a title\n```\n\n# Real title\n"
    meta = parse_note_metadata(note, vault_path="Ideas/x.md", fallback_created=FALLBACK)
    assert meta.title == "Real title"


def test_metadata_no_frontmatter_at_root_has_no_plane():
    # A file at the vault root (no folder) has no derivable plane.
    meta = parse_note_metadata("# Top", vault_path="loose.md", fallback_created=FALLBACK)
    assert meta.plane is None
    assert meta.planes == []
