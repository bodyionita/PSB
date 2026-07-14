"""Derived-profile tests (ADR-030 §4 / ADR-034, M3 task 6) — pure tiering/rendering + the refresh
service (tier selection, LLM-frugal skip, LLM/embed degrade), all against fakes (no live DB/LLM)."""

from __future__ import annotations

from datetime import date

import pytest

from app.config import Settings
from app.entities.entity_store import EntityRef, Neighbor
from app.entities.profile_refresh import ProfileRefreshService
from app.entities.profiles import (
    TIER_FULL,
    TIER_SNAPSHOT,
    TIER_STUB,
    choose_tier,
    mechanical_observations,
    neighborhood_hash,
    plan_profile,
    render_stub_profile,
)
from app.providers.registry import ProviderRegistry

from .fakes import (
    FakeAgentRunStore,
    FakeChatProvider,
    FakeEmbeddingProvider,
    FakeEntityStore,
    FakeProfileStore,
    fake_routing,
)


def _nb(node_id: str, rel: str, title: str, since: date | None = None, until: date | None = None):
    return Neighbor(
        node_id=node_id,
        type="memory",
        title=title,
        plane="personal",
        rel=rel,
        dir="in",
        since=since,
        until=until,
        occurred_start=None,
    )


# --- pure logic ---


def test_choose_tier_by_degree():
    assert choose_tier(1, snapshot_min=3, full_min=8) == TIER_STUB
    assert choose_tier(3, snapshot_min=3, full_min=8) == TIER_SNAPSHOT
    assert choose_tier(9, snapshot_min=3, full_min=8) == TIER_FULL


def test_mechanical_observations_carry_stamp_and_source():
    obs = mechanical_observations([_nb("m1", "involves", "Dinner", since=date(2025, 7, 10))])
    assert obs[0].node_ids == ["m1"]
    assert obs[0].render() == "[involves] Dinner (as of 2025-07-10)"


def test_stub_profile_lines():
    obs = mechanical_observations(
        [_nb("m1", "involves", "Dinner", since=date(2025, 7, 10)), _nb("m2", "about", "Chess")]
    )
    text = render_stub_profile(obs)
    assert "[about] Chess" in text
    assert "[involves] Dinner (as of 2025-07-10)" in text


def test_neighborhood_hash_is_stable_and_tier_sensitive():
    obs = mechanical_observations([_nb("m1", "involves", "Dinner")])
    h1 = neighborhood_hash(obs, TIER_STUB)
    assert h1 == neighborhood_hash(obs, TIER_STUB)  # stable
    assert h1 != neighborhood_hash(obs, TIER_SNAPSHOT)  # tier-sensitive


def test_plan_profile_degree_is_distinct_neighbors():
    plan = plan_profile(
        [_nb("m1", "involves", "A"), _nb("m1", "about", "A"), _nb("m2", "involves", "B")],
        snapshot_min=2,
        full_min=8,
    )
    # 2 distinct neighbors → snapshot tier.
    assert plan.tier == TIER_SNAPSHOT


# --- service ---


def _registry(*, chat_reply="Currently: a person.\n\n## Links\n- involves Dinner", chat_up=True):
    """Returns ``(registry, chat_provider)`` so a test can assert the LLM was / wasn't called."""
    chat = FakeChatProvider("fake-chat", reply=chat_reply, available=chat_up)
    registry = ProviderRegistry(
        {"fake-chat": chat, "fake-embed": FakeEmbeddingProvider(dim=4)},
        chat_chain=["fake-chat"],
        distill_chain=["fake-chat"],
        embedding_provider_id="fake-embed",
        stt_chain=[],
    )
    return registry, chat


def _service(entity_store, profile_store, registry, runs) -> ProfileRefreshService:
    return ProfileRefreshService(
        settings=Settings(profile_tier_snapshot_min=2, profile_tier_full_min=5),
        entity_store=entity_store,
        profile_store=profile_store,
        routing=fake_routing(registry),
        registry=registry,
        run_store=runs,
    )


def _entity(node_id="e1", title="Alex"):
    return EntityRef(
        id=node_id, type="person", title=title, aliases=["alex"], store_path="person/a.md"
    )


@pytest.mark.asyncio
async def test_stub_tier_skips_the_llm():
    store = FakeEntityStore(
        entities=[_entity()], neighborhoods={"e1": [_nb("m1", "involves", "Dinner")]}
    )
    profiles = FakeProfileStore()
    registry, chat = _registry()
    runs = FakeAgentRunStore()
    await _service(store, profiles, registry, runs).run_scheduled()

    # 1 neighbor < snapshot_min(2) → stub, mechanical text, no chat call.
    assert profiles.upserts[0]["tier"] == TIER_STUB
    assert "[involves] Dinner" in profiles.upserts[0]["profile"]
    assert chat.calls == 0
    assert profiles.upserts[0]["embedding"] is not None


@pytest.mark.asyncio
async def test_snapshot_tier_uses_the_llm():
    store = FakeEntityStore(
        entities=[_entity()],
        neighborhoods={"e1": [_nb("m1", "involves", "Dinner"), _nb("m2", "about", "Chess")]},
    )
    profiles = FakeProfileStore()
    runs = FakeAgentRunStore()
    await _service(store, profiles, _registry()[0], runs).run_scheduled()

    # 2 neighbors ≥ snapshot_min(2) → snapshot, the LLM text is stored.
    assert profiles.upserts[0]["tier"] == TIER_SNAPSHOT
    assert profiles.upserts[0]["profile"].startswith("Currently:")


@pytest.mark.asyncio
async def test_unchanged_neighborhood_is_skipped():
    neighbors = [_nb("m1", "involves", "Dinner")]
    store = FakeEntityStore(entities=[_entity()], neighborhoods={"e1": neighbors})
    # Seed the stored hash = the current neighborhood hash so the job skips it.
    plan = plan_profile(neighbors, snapshot_min=2, full_min=5)
    profiles = FakeProfileStore(hashes={"e1": plan.neighborhood_hash})
    runs = FakeAgentRunStore()
    await _service(store, profiles, _registry()[0], runs).run_scheduled()

    assert profiles.upserts == []  # nothing regenerated


@pytest.mark.asyncio
async def test_llm_down_degrades_to_stub_and_clears_hash():
    store = FakeEntityStore(
        entities=[_entity()],
        neighborhoods={"e1": [_nb("m1", "involves", "Dinner"), _nb("m2", "about", "Chess")]},
    )
    profiles = FakeProfileStore()
    runs = FakeAgentRunStore()
    await _service(store, profiles, _registry(chat_up=False)[0], runs).run_scheduled()

    # Snapshot-degree entity but the LLM is down → stub text + stub tier (so the badge matches the
    # content), hash cleared so the next run retries the synthesis.
    up = profiles.upserts[0]
    assert up["tier"] == TIER_STUB
    assert "[involves] Dinner" in up["profile"]
    assert up["neighborhood_hash"] == ""
