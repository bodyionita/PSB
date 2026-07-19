"""Orphan keep-list service tests (ADR-064 §5, M9.8 T5.5) — keep / list / un-keep + hubs-only guard.

The service is exercised against fakes (entity store + keep store); no live DB/LLM (08 policy). The
routing guards (unknown/tombstone → 404; content node → 400; unknown key → 404) are asserted as the
exceptions the admin router maps. Keying is checked to be **surface form + type** (survives a
reprocess) and the record to be an idempotent upsert.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.entities.entity_store import EntityNode
from app.entities.keep_store import KeepDecision, keep_key, surface_forms
from app.services.orphan_keep import (
    OrphanKeepIsContent,
    OrphanKeepKeyNotFound,
    OrphanKeepNotFound,
    OrphanKeepService,
)
from tests.fakes import FakeEntityStore, FakeKeepStore


def _service(entity_store, keeps) -> OrphanKeepService:
    return OrphanKeepService(
        settings=Settings(), entity_store=entity_store, keeps=keeps, vocab=None
    )


@pytest.mark.asyncio
async def test_keep_records_a_decision_keyed_on_surface_form_and_type():
    node = EntityNode("hub-1", "person", "Father", "person/father--hub-1.md", ["dad"], None)
    store = FakeEntityStore(nodes={"hub-1": node})
    keeps = FakeKeepStore()
    service = _service(store, keeps)

    decision = await service.keep("hub-1")

    assert decision.node_type == "person"
    # Normalized title + aliases, title first (ADR-064 §1 surface_forms).
    assert decision.forms == surface_forms("Father", ["dad"])
    assert decision.key == keep_key("person", ["father", "dad"])
    assert decision.node_id == "hub-1"
    # Persisted, and retrievable through list.
    listed = await service.list_keeps()
    assert [k.key for k in listed] == [decision.key]


@pytest.mark.asyncio
async def test_keep_is_idempotent_upsert():
    store = FakeEntityStore(
        nodes={"hub-1": EntityNode("hub-1", "person", "Mother", "person/m--hub-1.md", [], None)}
    )
    keeps = FakeKeepStore()
    service = _service(store, keeps)

    await service.keep("hub-1")
    await service.keep("hub-1")  # same identity → upsert, not a duplicate

    assert len(await service.list_keeps()) == 1


@pytest.mark.asyncio
async def test_keep_rejects_content_node():
    """A content (non-entity-like) node is not keepable — OrphanKeepIsContent (→400)."""
    store = FakeEntityStore(
        nodes={"mem-1": EntityNode("mem-1", "memory", "note", "memory/note--mem-1.md", [], None)}
    )
    service = _service(store, FakeKeepStore())
    with pytest.raises(OrphanKeepIsContent):
        await service.keep("mem-1")


@pytest.mark.asyncio
async def test_keep_rejects_unknown_and_tombstone():
    store = FakeEntityStore(
        nodes={"tomb": EntityNode("tomb", "person", "Old", "person/old--tomb.md", [], "surv-2")}
    )
    service = _service(store, FakeKeepStore())
    with pytest.raises(OrphanKeepNotFound):
        await service.keep("nope")  # unknown
    with pytest.raises(OrphanKeepNotFound):
        await service.keep("tomb")  # already a tombstone


def test_keep_key_is_url_path_safe():
    # The key travels in a URL path (DELETE /admin/orphan-keeps/{key}); the raw type/forms canonical
    # form carries a NUL separator (+ possibly a `/` inside a surface form) that Cloudflare/Caddy
    # would reject. base64url keeps it to the unreserved path alphabet so it round-trips.
    key = keep_key("topic", ["gluten-free / dairy-free", "gf"])
    assert key and all(c.isalnum() or c in "-_" for c in key)
    # Stable + type-scoped (a distinct type or form set yields a distinct key).
    assert key == keep_key("topic", ["gf", "gluten-free / dairy-free"])  # order-independent
    assert key != keep_key("person", ["gluten-free / dairy-free", "gf"])


@pytest.mark.asyncio
async def test_unkeep_removes_by_key_and_404s_on_unknown():
    key = keep_key("person", ["father"])
    keeps = FakeKeepStore(keeps=[KeepDecision(node_type="person", forms=["father"], node_id="x")])
    service = _service(FakeEntityStore(), keeps)

    await service.unkeep(key)
    assert await service.list_keeps() == []

    with pytest.raises(OrphanKeepKeyNotFound):
        await service.unkeep(key)  # already gone
