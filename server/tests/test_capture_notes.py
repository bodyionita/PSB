"""Tests for note rendering + the filesystem NoteWriter (tmp vault, no mocks)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.capture.notes import (
    NoteWriter,
    note_filename,
    render_note,
    sanitize_title,
)
from app.capture.organizer import OrganizerNote

CREATED = datetime(2026, 7, 12, 9, 14, 3, tzinfo=UTC)


# --- pure helpers -----------------------------------------------------------------------


def test_sanitize_strips_illegal_chars_and_collapses_whitespace():
    assert sanitize_title('a/b:c*?  "d"') == "a b c d"


def test_sanitize_never_empty_or_trailing_dot():
    assert sanitize_title("...") == "Untitled"
    assert sanitize_title("report.") == "report"
    assert sanitize_title("   ") == "Untitled"


def test_note_filename_shape():
    assert note_filename(CREATED, "Q3 Plan") == "2026-07-12 Q3 Plan.md"


def test_render_note_has_frontmatter_h1_and_related():
    note = OrganizerNote(
        title="Dinner with Alex",
        plane="Friends",
        planes=("Friends", "Personal"),
        tags=("warmth", "reconnecting"),
        body="We finally caught up.",
    )
    text = render_note(
        note,
        note_id="cap-1",
        created_local=CREATED,
        source="text",
        source_ref=None,
        related=("Personal/2026-07-12 Feeling nostalgic.md",),
    )
    assert text.startswith("---\n")
    assert "id: cap-1" in text
    assert "created: 2026-07-12T09:14:03+00:00" in text
    assert "source: text" in text
    assert "source_ref:" not in text  # omitted when absent
    assert "plane: Friends" in text
    assert "planes: [Friends, Personal]" in text
    assert 'tags: [warmth, reconnecting]' in text
    assert "# Dinner with Alex" in text
    assert "## Related" in text
    assert "[[Personal/2026-07-12 Feeling nostalgic]]" in text


def test_render_note_quotes_paths_with_spaces_in_related():
    note = OrganizerNote("t", "Ideas", ("Ideas",), (), "b")
    text = render_note(
        note,
        note_id="x",
        created_local=CREATED,
        source="text",
        source_ref=None,
        related=("Ideas/2026-07-12 A B.md",),
    )
    assert 'related: ["Ideas/2026-07-12 A B.md"]' in text


# --- NoteWriter -------------------------------------------------------------------------


def test_write_single_note_atomic(tmp_path: Path):
    writer = NoteWriter(str(tmp_path))
    note = OrganizerNote("My Idea", "Ideas", ("Ideas",), ("spark",), "body here")
    paths = writer.write_notes([note], capture_id="c1", created_local=CREATED, source="text")
    assert paths == ["Ideas/2026-07-12 My Idea.md"]
    written = (tmp_path / "Ideas" / "2026-07-12 My Idea.md").read_text(encoding="utf-8")
    assert "# My Idea" in written
    # No leftover temp files.
    assert list((tmp_path / "Ideas").glob(".*.tmp")) == []


def test_write_siblings_cross_link(tmp_path: Path):
    writer = NoteWriter(str(tmp_path))
    notes = [
        OrganizerNote("Work item", "Professional", ("Professional",), (), "a"),
        OrganizerNote("Personal thread", "Personal", ("Personal",), (), "b"),
    ]
    paths = writer.write_notes(notes, capture_id="c2", created_local=CREATED, source="voice")
    assert len(paths) == 2
    work = (tmp_path / paths[0]).read_text(encoding="utf-8")
    personal = (tmp_path / paths[1]).read_text(encoding="utf-8")
    # Each references the other via related frontmatter + wikilink.
    assert "Personal/2026-07-12 Personal thread.md" in work
    assert "[[Personal/2026-07-12 Personal thread]]" in work
    assert "Professional/2026-07-12 Work item.md" in personal


def test_filename_collisions_get_numeric_suffix(tmp_path: Path):
    writer = NoteWriter(str(tmp_path))
    note = OrganizerNote("Same", "Ideas", ("Ideas",), (), "body")
    p1 = writer.write_notes([note], capture_id="a", created_local=CREATED, source="text")
    p2 = writer.write_notes([note], capture_id="b", created_local=CREATED, source="text")
    assert p1 == ["Ideas/2026-07-12 Same.md"]
    assert p2 == ["Ideas/2026-07-12 Same 2.md"]
    assert (tmp_path / p2[0]).exists()


def test_sibling_collision_within_one_batch(tmp_path: Path):
    writer = NoteWriter(str(tmp_path))
    notes = [
        OrganizerNote("Dup", "Ideas", ("Ideas",), (), "a"),
        OrganizerNote("Dup", "Ideas", ("Ideas",), (), "b"),
    ]
    paths = writer.write_notes(notes, capture_id="c", created_local=CREATED, source="text")
    assert paths == ["Ideas/2026-07-12 Dup.md", "Ideas/2026-07-12 Dup 2.md"]


def test_remove_notes_unlinks_and_tolerates_missing(tmp_path: Path):
    writer = NoteWriter(str(tmp_path))
    note = OrganizerNote("Gone", "Ideas", ("Ideas",), (), "body")
    paths = writer.write_notes([note], capture_id="c", created_local=CREATED, source="text")
    writer.remove_notes(paths + ["Ideas/does-not-exist.md"])
    assert not (tmp_path / paths[0]).exists()
