"""Pure-logic tests for the organizer (no I/O, no mocks) — 08 testing policy."""

from __future__ import annotations

from app.capture.organizer import (
    inbox_fallback_note,
    parse_organizer_json,
    validate_organizer_output,
)

PLANES = ["Professional", "Personal", "Ideas"]
INBOX = "Inbox"


def _validate(parsed):
    return validate_organizer_output(
        parsed, planes=PLANES, inbox_plane=INBOX, max_notes=8, max_tags=12
    )


# --- parse_organizer_json ---------------------------------------------------------------


def test_parse_plain_json():
    assert parse_organizer_json('{"notes": []}') == {"notes": []}


def test_parse_strips_code_fences():
    text = '```json\n{"notes": [{"title": "x"}]}\n```'
    assert parse_organizer_json(text) == {"notes": [{"title": "x"}]}


def test_parse_extracts_object_from_surrounding_prose():
    text = 'Sure! Here you go:\n{"notes": []}\nHope that helps.'
    assert parse_organizer_json(text) == {"notes": []}


def test_parse_returns_none_for_garbage():
    assert parse_organizer_json("not json at all") is None
    assert parse_organizer_json("") is None


# --- validate_organizer_output ----------------------------------------------------------


def test_valid_note_normalises_plane_casing():
    # Model returned lower-case "professional"; canonical config spelling is restored.
    notes = _validate({"notes": [{"title": "Q3 plan", "plane": "professional", "body": "text"}]})
    assert len(notes) == 1
    assert notes[0].plane == "Professional"
    assert notes[0].planes == ("Professional",)  # planes defaults to a superset of plane


def test_unknown_plane_falls_back_to_inbox():
    notes = _validate({"notes": [{"title": "t", "plane": "Nonsense", "body": "b"}]})
    assert notes[0].plane == INBOX


def test_planes_filtered_and_superset_of_primary():
    notes = _validate(
        {
            "notes": [
                {
                    "title": "t",
                    "plane": "Personal",
                    "planes": ["personal", "ideas", "bogus"],
                    "body": "b",
                }
            ]
        }
    )
    assert notes[0].plane == "Personal"
    assert notes[0].planes == ("Personal", "Ideas")  # bogus dropped, primary first


def test_tags_cleaned_lowercased_deduped_and_capped():
    parsed = {
        "notes": [
            {
                "title": "t",
                "plane": "Ideas",
                "tags": ["#Focus", "focus", "Energy", 5, "  "],
                "body": "b",
            }
        ]
    }
    notes = validate_organizer_output(
        parsed, planes=PLANES, inbox_plane=INBOX, max_notes=8, max_tags=2
    )
    assert notes[0].tags == ("focus", "energy")


def test_notes_capped_at_max():
    parsed = {
        "notes": [{"title": f"t{i}", "plane": "Ideas", "body": "b"} for i in range(20)]
    }
    notes = validate_organizer_output(
        parsed, planes=PLANES, inbox_plane=INBOX, max_notes=3, max_tags=12
    )
    assert len(notes) == 3


def test_notes_missing_title_or_body_are_dropped():
    parsed = {
        "notes": [
            {"title": "", "plane": "Ideas", "body": "b"},
            {"title": "t", "plane": "Ideas", "body": "   "},
            {"plane": "Ideas", "body": "b"},
            {"title": "good", "plane": "Ideas", "body": "keeps"},
        ]
    }
    notes = _validate(parsed)
    assert [n.title for n in notes] == ["good"]


def test_empty_or_malformed_returns_no_notes():
    assert _validate(None) == ()
    assert _validate({"notes": "nope"}) == ()
    assert _validate({}) == ()


# --- inbox_fallback_note ----------------------------------------------------------------


def test_inbox_fallback_uses_first_eight_words_and_full_body():
    raw = "one two three four five six seven eight nine ten"
    note = inbox_fallback_note(raw, inbox_plane=INBOX)
    assert note.title == "one two three four five six seven eight"
    assert note.body == raw
    assert note.plane == INBOX
    assert note.planes == (INBOX,)


def test_inbox_fallback_handles_empty_input():
    note = inbox_fallback_note("   ", inbox_plane=INBOX)
    assert note.title == "Untitled capture"
    assert note.body == "(empty capture)"
