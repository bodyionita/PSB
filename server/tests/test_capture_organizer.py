"""Pure-logic tests for the organizer v3 (no I/O, no mocks) — 08 testing policy."""

from __future__ import annotations

from app.capture.organizer import (
    inbox_fallback_node,
    parse_organizer_json,
    validate_organizer_output,
)

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


def _validate(parsed, *, max_nodes=8, max_tags=12, max_edges=12):
    return validate_organizer_output(
        parsed,
        planes=PLANES,
        node_types=NODE_TYPES,
        edge_rels=EDGE_RELS,
        entity_types=ENTITY_TYPES,
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


# --- occurred + entities ----------------------------------------------------------------


def test_occurred_kept_only_when_valid_partial_iso():
    good, _, _ = _validate(
        {"nodes": [{"title": "t", "type": "memory", "occurred": "2025-07", "body": "b"}]}
    )
    assert good[0].occurred == "2025-07"
    bad, _, _ = _validate(
        {"nodes": [{"title": "t", "type": "memory", "occurred": "last summer", "body": "b"}]}
    )
    assert bad[0].occurred is None


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
