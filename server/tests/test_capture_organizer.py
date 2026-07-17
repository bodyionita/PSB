"""Pure-logic tests for the organizer (no I/O, no mocks) — 08 testing policy.

v6 (M8.2): symbolic time-references resolved against the stored anchor into ``occurred`` +
``[[t:…]]`` body tokens (ADR-056), the ``interiority`` stamp, and inner-voice ``arose_from``
extraction (ADR-055)."""

from __future__ import annotations

from datetime import datetime

from app.capture.organizer import (
    inbox_fallback_node,
    parse_organizer_json,
    render_anchor,
    validate_organizer_output,
)

# A fixed FRIDAY anchor (matches tests/test_temporal.py) so relative-date resolution is
# deterministic: "10 days ago" → 2026-07-07, "last summer" → summer 2025.
ANCHOR = datetime(2026, 7, 17, 8, 40)

PLANES = ["Professional", "Personal", "Ideas"]
NODE_TYPES = [
    "memory",
    "person",
    "idea",
    "conversation",
    "insight",
    "place",
    "event",
    "project",
    "topic",
]
EDGE_RELS = ["involves", "about", "part_of", "led_to", "follows", "at"]
# The entity-hub types (ADR-038/039). `idea` is a CONTENT type — not here (M3 task 11).
ENTITY_TYPES = ["person", "place", "topic", "event", "project"]


def _validate(parsed, *, anchor=ANCHOR, max_nodes=8, max_tags=12, max_edges=12):
    return validate_organizer_output(
        parsed,
        planes=PLANES,
        node_types=NODE_TYPES,
        edge_rels=EDGE_RELS,
        entity_types=ENTITY_TYPES,
        anchor=anchor,
        max_nodes=max_nodes,
        max_tags=max_tags,
        max_edges=max_edges,
    )


# --- parse_organizer_json ---------------------------------------------------------------


def test_parse_plain_json():
    assert parse_organizer_json('{"nodes": []}') == {"nodes": []}


def test_parse_strips_code_fences():
    text = '```json\n{"nodes": [{"title": "x"}]}\n```'
    assert parse_organizer_json(text) == {"nodes": [{"title": "x"}]}


def test_parse_extracts_object_from_surrounding_prose():
    text = 'Sure! Here you go:\n{"nodes": []}\nHope that helps.'
    assert parse_organizer_json(text) == {"nodes": []}


def test_parse_returns_none_for_garbage():
    assert parse_organizer_json("not json at all") is None
    assert parse_organizer_json("") is None


# --- validate_organizer_output: typing + vocab proposals --------------------------------


def test_valid_node_keeps_known_type():
    nodes, proposals, _ = _validate(
        {"nodes": [{"title": "Q3 plan", "type": "idea", "plane": "professional", "body": "text"}]}
    )
    assert len(nodes) == 1
    assert nodes[0].type == "idea"
    assert nodes[0].plane == "Professional"  # canonical spelling restored
    assert nodes[0].planes == ("Professional",)
    assert proposals == ()


def test_unknown_type_coerces_to_memory_and_files_a_proposal():
    nodes, proposals, _ = _validate(
        {"nodes": [{"title": "t", "type": "recipe", "plane": "Ideas", "body": "b"}]}
    )
    assert nodes[0].type == "memory"
    assert {"vocab": "node_type", "value": "recipe"} in proposals


def test_missing_type_defaults_to_memory():
    nodes, _, _ = _validate({"nodes": [{"title": "t", "plane": "Ideas", "body": "b"}]})
    assert nodes[0].type == "memory"


def test_unknown_plane_becomes_none():
    nodes, _, _ = _validate({"nodes": [{"title": "t", "plane": "Nonsense", "body": "b"}]})
    assert nodes[0].plane is None  # no inbox-plane fallback anymore; plane is optional
    assert nodes[0].planes == ()


def test_planes_filtered_and_superset_of_primary():
    nodes, _, _ = _validate(
        {
            "nodes": [
                {
                    "title": "t",
                    "plane": "Personal",
                    "planes": ["personal", "ideas", "bogus"],
                    "body": "b",
                }
            ]
        }
    )
    assert nodes[0].plane == "Personal"
    assert nodes[0].planes == ("Personal", "Ideas")  # bogus dropped, primary first


# --- temporal: symbolic time-references → occurred + body tokens (ADR-056) --------------


def _node(**over):
    """A minimal valid content node dict, overridable per field."""
    return {"nodes": [{"title": "t", "type": "memory", "body": "b", **over}]}


def test_event_time_ref_sets_occurred_and_tokenizes_body():
    # "10 days ago" against the Friday 2026-07-17 anchor → 2026-07-07, day-granular occurred, and
    # the body phrase is replaced by its token (code computes — the LLM only classified).
    nodes, _, _ = _validate(
        _node(
            body="Walked with D. 10 days ago, felt easy.",
            time_refs=[
                {
                    "phrase": "10 days ago",
                    "kind": "relative",
                    "unit": "day",
                    "offset": -10,
                    "event": True,
                }
            ],
        )
    )
    assert nodes[0].occurred == "2026-07-07"
    assert nodes[0].occurred_end is None
    assert nodes[0].body == "Walked with D. [[t:2026-07-07]], felt easy."


def test_season_event_yields_labeled_range_token_and_occurred_range():
    # "last summer" → summer 2025 (northern hemisphere) → a labeled range token + range occurred.
    nodes, _, _ = _validate(
        _node(
            body="We met last summer.",
            time_refs=[
                {
                    "phrase": "last summer",
                    "kind": "season",
                    "season": "summer",
                    "year_offset": -1,
                    "event": True,
                }
            ],
        )
    )
    assert nodes[0].occurred == "2025-06"
    assert nodes[0].occurred_end == "2025-08"
    assert nodes[0].body == "We met [[t:2025-06/2025-08|summer 2025]]."


def test_non_event_ref_tokenizes_but_leaves_occurred_unset():
    nodes, _, _ = _validate(
        _node(
            body="I keep thinking about yesterday.",
            time_refs=[{"phrase": "yesterday", "kind": "relative", "unit": "day", "offset": -1}],
        )
    )
    assert nodes[0].occurred is None
    assert nodes[0].body == "I keep thinking about [[t:2026-07-16]]."


def test_unresolvable_ref_stays_prose_and_leaves_occurred_unset():
    # A malformed symbolic form must NOT produce a token or a guessed date (fail-closed, rule 12).
    nodes, _, _ = _validate(
        _node(
            body="Something happened at some point.",
            time_refs=[{"phrase": "at some point", "kind": "bogus", "event": True}],
        )
    )
    assert nodes[0].occurred is None
    assert nodes[0].body == "Something happened at some point."  # untouched


def test_phrase_absent_from_body_still_sets_occurred_without_a_token():
    # The event date is set from the resolved ref even if its phrase isn't found verbatim in body.
    nodes, _, _ = _validate(
        _node(
            body="An old memory.",
            time_refs=[
                {
                    "phrase": "ten days ago",
                    "kind": "relative",
                    "unit": "day",
                    "offset": -10,
                    "event": True,
                }
            ],
        )
    )
    assert nodes[0].occurred == "2026-07-07"
    assert "[[t:" not in nodes[0].body


def test_no_time_refs_leaves_occurred_none():
    nodes, _, _ = _validate(_node(body="A timeless thought."))
    assert nodes[0].occurred is None
    assert nodes[0].occurred_end is None


def test_sub_day_event_keeps_occurred_date_granular_token_owns_time():
    # ADR-056 §6: occurred_* stay DATE-granular even for a minute-precise event — the time-of-day
    # lives only in the body token, never in occurred.
    nodes, _, _ = _validate(
        _node(
            body="Call at 10pm.",
            time_refs=[
                {
                    "phrase": "at 10pm",
                    "kind": "explicit",
                    "year": 2025,
                    "month": 7,
                    "day": 7,
                    "hour": 22,
                    "minute": 0,
                    "event": True,
                }
            ],
        )
    )
    assert nodes[0].occurred == "2025-07-07"  # no T..:.. — date-granular
    assert "T" not in nodes[0].occurred
    assert "[[t:2025-07-07T22:00]]" in nodes[0].body  # the token owns the sub-day precision


def test_anchor_determinism_two_anchors_resolve_differently():
    # The SAME symbolic "10 days ago" resolves against whatever anchor is passed — reprocess
    # determinism (ADR-056 §1): the stored anchor, never wall-clock.
    payload = _node(
        body="x 10 days ago",
        time_refs=[
            {
                "phrase": "10 days ago",
                "kind": "relative",
                "unit": "day",
                "offset": -10,
                "event": True,
            }
        ],
    )
    a, _, _ = _validate(payload, anchor=datetime(2026, 7, 17))
    b, _, _ = _validate(payload, anchor=datetime(2020, 1, 20))
    assert a[0].occurred == "2026-07-07"
    assert b[0].occurred == "2020-01-10"


# --- interiority (ADR-055 §1) -----------------------------------------------------------


def test_interiority_defaults_to_external_when_absent_or_unknown():
    absent, _, _ = _validate(_node())
    assert absent[0].interiority == "external"
    unknown, _, _ = _validate(_node(interiority="deep"))
    assert unknown[0].interiority == "external"


def test_interiority_kept_when_valid_and_case_folded():
    for raw, expected in (("internal", "internal"), ("MIXED", "mixed"), ("External", "external")):
        nodes, _, _ = _validate(_node(interiority=raw))
        assert nodes[0].interiority == expected


# --- inner-voice extraction: arose_from (ADR-055 §2) ------------------------------------


def test_arose_from_remapped_to_surviving_result_index():
    # Node 0 = event (external), node 1 = feeling (internal) arising from index 0.
    nodes, _, _ = _validate(
        {
            "nodes": [
                {
                    "title": "Walk",
                    "type": "memory",
                    "body": "Walked with D.",
                    "interiority": "external",
                },
                {
                    "title": "Ease",
                    "type": "memory",
                    "body": "It felt easy.",
                    "interiority": "internal",
                    "arose_from": 0,
                },
            ]
        }
    )
    assert nodes[1].interiority == "internal"
    assert nodes[1].arose_from == 0
    assert nodes[0].arose_from is None


def test_arose_from_dangling_or_self_reference_drops_to_none():
    # A self-reference and an out-of-range index both become None (no bogus edge downstream).
    nodes, _, _ = _validate(
        {
            "nodes": [
                {"title": "a", "type": "memory", "body": "b", "arose_from": 0},  # self
                {"title": "c", "type": "memory", "body": "d", "arose_from": 9},  # out of range
            ]
        }
    )
    assert nodes[0].arose_from is None
    assert nodes[1].arose_from is None


def test_arose_from_reindexed_when_an_earlier_node_is_dropped():
    # The first raw node is dropped (empty body); the LLM's arose_from=0 must remap to the
    # surviving event node's NEW result index, not the raw index.
    nodes, _, _ = _validate(
        {
            "nodes": [
                {"title": "dropped", "body": "   "},  # raw 0 → dropped
                {"title": "event", "type": "memory", "body": "the walk"},  # raw 1 → result 0
                {
                    "title": "feeling",
                    "type": "memory",
                    "body": "felt good",  # raw 2 → result 1
                    "interiority": "internal",
                    "arose_from": 1,
                },
            ]
        }
    )
    assert [n.title for n in nodes] == ["event", "feeling"]
    assert nodes[1].arose_from == 0  # remapped from raw index 1 → result index 0


def test_arose_from_non_integer_is_ignored():
    nodes, _, _ = _validate(_node(interiority="internal", arose_from="not-a-number"))
    assert nodes[0].arose_from is None


# --- anchor rendering -------------------------------------------------------------------


def test_render_anchor_states_the_recorded_weekday_and_time():
    line = render_anchor(datetime(2026, 7, 17, 8, 40), "Europe/Bucharest")
    assert "Friday, 2026-07-17 08:40 (Europe/Bucharest)" in line
    assert "recorded on" in line


def test_entities_kept_with_known_type_and_rel():
    nodes, _, _ = _validate(
        {
            "nodes": [
                {
                    "title": "Dinner",
                    "type": "memory",
                    "body": "b",
                    "entities": [
                        {"name": "Alex", "type": "person", "rel": "involves", "disambig": "brother"}
                    ],
                }
            ]
        }
    )
    m = nodes[0].entities[0]
    assert (m.name, m.type, m.rel, m.disambig) == ("Alex", "person", "involves", "brother")


def test_entity_unknown_type_or_rel_is_dropped_with_a_proposal():
    nodes, proposals, _ = _validate(
        {
            "nodes": [
                {
                    "title": "t",
                    "type": "memory",
                    "body": "b",
                    "entities": [
                        {"name": "X", "type": "gadget", "rel": "involves"},
                        {"name": "Y", "type": "person", "rel": "owns"},
                    ],
                }
            ]
        }
    )
    assert nodes[0].entities == ()  # both dropped
    assert {"vocab": "entity_type", "value": "gadget"} in proposals
    assert {"vocab": "edge_rel", "value": "owns"} in proposals


def test_tags_cleaned_lowercased_deduped_and_capped():
    nodes, _, _ = _validate(
        {
            "nodes": [
                {
                    "title": "t",
                    "plane": "Ideas",
                    "tags": ["#Focus", "focus", "Energy", 5, "  "],
                    "body": "b",
                }
            ]
        },
        max_tags=2,
    )
    assert nodes[0].tags == ("focus", "energy")


def test_nodes_capped_at_max():
    parsed = {"nodes": [{"title": f"t{i}", "plane": "Ideas", "body": "b"} for i in range(20)]}
    nodes, _, _ = _validate(parsed, max_nodes=3)
    assert len(nodes) == 3


def test_nodes_missing_title_or_body_are_dropped():
    parsed = {
        "nodes": [
            {"title": "", "body": "b"},
            {"title": "t", "body": "   "},
            {"body": "b"},
            {"title": "good", "body": "keeps"},
        ]
    }
    nodes, _, _ = _validate(parsed)
    assert [n.title for n in nodes] == ["good"]


def test_empty_or_malformed_returns_no_nodes():
    assert _validate(None) == ((), (), ())
    assert _validate({"nodes": "nope"}) == ((), (), ())
    assert _validate({}) == ((), (), ())


# --- entity-type coercion guard (ADR-039, M3 task 11) -----------------------------------


def test_entity_typed_content_node_is_coerced_to_memory():
    # The organizer must never emit a person/place/… as a content node; the structural guard
    # coerces it to memory, keeping the body + entities so the narrative + mentions survive.
    nodes, proposals, coerced = _validate(
        {
            "nodes": [
                {
                    "title": "How I know Horia",
                    "type": "person",
                    "body": "Horia is the husband of Madalina.",
                    "entities": [{"name": "Horia", "type": "person", "rel": "involves"}],
                }
            ]
        }
    )
    assert nodes[0].type == "memory"  # coerced
    assert nodes[0].body == "Horia is the husband of Madalina."  # content kept
    assert nodes[0].entities[0].name == "Horia"  # mentions kept → hub still minted downstream
    assert coerced == ("person",)
    assert proposals == ()  # coercion is not a vocab proposal (the type is known, just misused)


def test_each_entity_type_is_coerced():
    for etype in ENTITY_TYPES:
        nodes, _, coerced = _validate({"nodes": [{"title": "t", "type": etype, "body": "b"}]})
        assert nodes[0].type == "memory", etype
        assert coerced == (etype,)


def test_content_types_are_not_coerced():
    for ctype in ("memory", "idea", "insight", "conversation"):
        nodes, _, coerced = _validate({"nodes": [{"title": "t", "type": ctype, "body": "b"}]})
        assert nodes[0].type == ctype, ctype
        assert coerced == ()


# --- inbox_fallback_node ----------------------------------------------------------------


def test_inbox_fallback_uses_first_eight_words_and_full_body():
    raw = "one two three four five six seven eight nine ten"
    node = inbox_fallback_node(raw)
    assert node.title == "one two three four five six seven eight"
    assert node.body == raw
    assert node.type == "memory"
    assert node.in_inbox is True
    assert node.plane is None


def test_inbox_fallback_handles_empty_input():
    node = inbox_fallback_node("   ")
    assert node.title == "Untitled capture"
    assert node.body == "(empty capture)"
