"""Pure-logic tests for the M8.1 T4 ``node_refs`` decode (ADR-054 §5 replan).

``PgCaptureStore.get``/``list_recent`` resolve ``node_paths -> nodes.id`` via a real-DB LATERAL
join (SQL-only, covered by the real-PG smoke — 08 testing policy); ``_node_refs`` is the pure
decode of that jsonb aggregate (asyncpg hands jsonb back as text), unit-testable with no DB.
"""

from __future__ import annotations

import json

from app.services.capture_store import CaptureNodeRef, _node_refs


def test_node_refs_none_is_empty() -> None:
    # A capture with no node_paths (or none yet resolved to a live node) — `jsonb_agg` over zero
    # rows is SQL NULL, not `[]`.
    assert _node_refs(None) == []


def test_node_refs_decodes_json_text() -> None:
    # asyncpg's default jsonb codec hands the aggregate back as a JSON string.
    raw = json.dumps(
        [
            {"id": "n1", "store_path": "memory/a.md", "type": "memory", "title": "A"},
            {"id": "n2", "store_path": "person/b.md", "type": "person", "title": None},
        ]
    )
    assert _node_refs(raw) == [
        CaptureNodeRef(id="n1", store_path="memory/a.md", type="memory", title="A"),
        CaptureNodeRef(id="n2", store_path="person/b.md", type="person", title=None),
    ]


def test_node_refs_tolerates_already_decoded_list() -> None:
    # Mirrors the `_details`/`graph_health` jsonb-decode convention — tolerate a pre-decoded value
    # too (a differently-configured codec, or a fake in a future test).
    already = [{"id": "n1", "store_path": "idea/x.md", "type": "idea", "title": "X"}]
    assert _node_refs(already) == [
        CaptureNodeRef(id="n1", store_path="idea/x.md", type="idea", title="X"),
    ]


def test_node_refs_coerces_id_to_str() -> None:
    # `n.id` is a Postgres uuid; `jsonb_build_object` renders it as a JSON string already, but the
    # decode is defensive (mirrors `_record`'s `str(row["id"])`) in case a future codec hands back
    # a UUID object inside the aggregate.
    raw = [{"id": 123, "store_path": "memory/a.md", "type": None, "title": None}]
    assert _node_refs(raw)[0].id == "123"
