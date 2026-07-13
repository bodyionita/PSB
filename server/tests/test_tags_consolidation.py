"""Pure tag-consolidation logic (ADR-024 §2) — no mocks (08 testing policy)."""

from __future__ import annotations

from app.capture.organizer import render_tag_vocabulary
from app.tags.consolidation import (
    TagMerge,
    build_tag_mapping,
    clean_merges,
    parse_merge_plan,
    remap_tags,
    rewrite_note_tags,
)

# --- organizer vocabulary injection (ADR-024 §1) ------------------------------------------------


def test_render_tag_vocabulary_empty_collapses():
    assert render_tag_vocabulary([]) == ""


def test_render_tag_vocabulary_lists_tags_most_used_first():
    block = render_tag_vocabulary(["work", "calm", "health"])
    assert "prefer reusing" in block
    assert "work, calm, health" in block


# --- parse_merge_plan ---------------------------------------------------------------------------


def test_parse_merge_plan_tolerates_code_fence():
    text = '```json\n{"merges": [{"canonical": "second-brain", "variants": ["secondbrain"]}]}\n```'
    assert parse_merge_plan(text) == [("second-brain", ["secondbrain"])]


def test_parse_merge_plan_rejects_non_conforming():
    assert parse_merge_plan("not json") == []
    assert parse_merge_plan('{"merges": "nope"}') == []
    assert parse_merge_plan('{"merges": [{"canonical": 3}]}') == []


# --- clean_merges -------------------------------------------------------------------------------


def test_clean_merges_slugifies_and_drops_unknown_when_allowed_given():
    allowed = {"second-brain": 5, "secondbrain": 2}
    merges = clean_merges(
        [("Second Brain", ["secondbrain", "totally-made-up"])], allowed=allowed
    )
    # "Second Brain" slugs to "second-brain" (known); the hallucinated tag is dropped.
    assert merges == [TagMerge(canonical="second-brain", variants=("secondbrain",))]


def test_clean_merges_needs_two_distinct_members():
    assert clean_merges([("solo", [])], allowed=None) == []
    assert clean_merges([("dup", ["dup"])], allowed=None) == []


def test_clean_merges_reassigns_canonical_to_highest_frequency_when_given_one_invalid():
    allowed = {"secondbrain": 9, "second-brain-app": 1}
    # canonical "second-brain" isn't in the vocabulary → pick the most-used member.
    merges = clean_merges(
        [("second-brain", ["secondbrain", "second-brain-app"])], allowed=allowed
    )
    assert merges == [
        TagMerge(canonical="secondbrain", variants=("second-brain-app",))
    ]


def test_clean_merges_no_tag_maps_to_two_canonicals():
    # "x" appears in two groups; the second group loses it and then has too few members.
    merges = clean_merges([("a", ["x"]), ("b", ["x"])], allowed=None)
    assert merges == [TagMerge(canonical="a", variants=("x",))]


# --- mapping + remap ----------------------------------------------------------------------------


def test_build_tag_mapping_and_remap_dedupes_preserving_order():
    merges = [TagMerge(canonical="second-brain", variants=("secondbrain", "sb"))]
    mapping = build_tag_mapping(merges)
    assert mapping == {"secondbrain": "second-brain", "sb": "second-brain"}
    # both variants collapse onto the canonical, which is de-duplicated with an existing one.
    assert remap_tags(["sb", "calm", "secondbrain"], mapping) == ["second-brain", "calm"]


# --- rewrite_note_tags --------------------------------------------------------------------------

_NOTE = """\
---
id: abc
plane: Ideas
planes: [Ideas]
tags: [secondbrain, calm]
related: []
---

# A thought

body text
"""


def test_rewrite_note_tags_replaces_variant_in_inline_list():
    new, changed = rewrite_note_tags(_NOTE, {"secondbrain": "second-brain"})
    assert changed is True
    assert "tags: [second-brain, calm]" in new
    # only the tags line changed; body + other keys intact.
    assert "# A thought" in new and "plane: Ideas" in new


def test_rewrite_note_tags_no_change_returns_original_verbatim():
    original = _NOTE.replace("\n", "\r\n")  # CRLF vault file
    new, changed = rewrite_note_tags(original, {"nonexistent": "x"})
    assert changed is False
    assert new == original  # untouched bytes, no newline normalization when nothing changed


def test_rewrite_note_tags_handles_scalar_tags_value():
    note = "---\ntags: secondbrain\n---\n\nbody\n"
    new, changed = rewrite_note_tags(note, {"secondbrain": "second-brain"})
    assert changed is True
    assert "tags: [second-brain]" in new


def test_rewrite_note_tags_without_frontmatter_is_noop():
    note = "# no frontmatter\n\nbody with a #secondbrain mention\n"
    new, changed = rewrite_note_tags(note, {"secondbrain": "second-brain"})
    assert changed is False
    assert new == note


def test_rewrite_note_tags_ignores_non_toplevel_tags_key():
    # An indented "tags:" (e.g. inside a body code block after a stray fence) is not the key.
    note = "---\nplane: Ideas\n---\n\nbody\n  tags: [secondbrain]\n"
    new, changed = rewrite_note_tags(note, {"secondbrain": "second-brain"})
    assert changed is False
