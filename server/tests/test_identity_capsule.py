"""IdentityCapsuleService tests (M5 task 2, ADR-046 §5 / ADR-033 #1).

The nightly/on-demand distiller: blend high-degree hubs + recent memories + recent insights → one
``conspect`` call → a ~300-token capsule blob in ``app_settings``. Fakes only, no live LLM/DB (08
testing policy). Covers the happy path (source fenced, blob saved, run succeeded), the best-effort
skips that keep the last capsule (no source / LLM down / empty), the char cap, source-ref
provenance, single-flight on the on-demand trigger, and the blob store's encode/decode.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.identity.service import AGENT, CapsuleOutcome, IdentityCapsuleService
from app.identity.store import CapsuleBlob, HubProfile, RecentNode, _decode_blob, _encode_blob
from app.providers.registry import ProviderRegistry
from app.services.agent_runs import SUCCEEDED

from .fakes import (
    FakeAgentRunStore,
    FakeCapsuleSourceStore,
    FakeCapsuleStore,
    FakeChatProvider,
    fake_routing,
)


def _hub(node_id: str, title: str, *, degree: int = 3, profile: str = "") -> HubProfile:
    return HubProfile(
        node_id=node_id,
        title=title,
        type="person",
        tier="snapshot",
        profile=profile or f"{title} is someone the user knows.",
        degree=degree,
    )


def _node(node_id: str, title: str, *, node_type: str = "memory", excerpt: str = "") -> RecentNode:
    return RecentNode(
        node_id=node_id,
        title=title,
        type=node_type,
        plane="Personal",
        excerpt=excerpt or f"something about {title}",
    )


def _make(
    *,
    hubs=None,
    memories=None,
    insights=None,
    reply: str = "The user is a builder who works with Alex and Bob on the brain project.",
    available: bool = True,
    settings: Settings | None = None,
    clock=None,
):
    provider = FakeChatProvider("conspect-p", reply=reply, available=available)
    registry = ProviderRegistry(
        {"conspect-p": provider},
        chat_chain=["conspect-p"],
        distill_chain=["conspect-p"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    settings = settings or Settings(
        chat_chain=["conspect-p"], distill_chain=["conspect-p"], quick_chain=["conspect-p"]
    )
    routing = fake_routing(registry, chain=("conspect-p",))
    capsule = FakeCapsuleStore()
    sources = FakeCapsuleSourceStore(hubs=hubs, memories=memories, insights=insights)
    runs = FakeAgentRunStore()
    service = IdentityCapsuleService(
        settings=settings,
        capsule_store=capsule,
        sources=sources,
        routing=routing,
        run_store=runs,
        clock=clock,
    )
    return service, capsule, sources, provider, runs


# --- happy path -----------------------------------------------------------------------------------


async def test_refresh_distills_and_saves_blob():
    fixed = datetime(2026, 7, 15, 4, 35, tzinfo=UTC)
    service, capsule, sources, provider, runs = _make(
        hubs=[_hub("h1", "Alex", degree=9), _hub("h2", "Bob", degree=4)],
        memories=[_node("m1", "Shipped the MCP server")],
        insights=[_node("i1", "Prefers async", node_type="insight")],
        clock=lambda: fixed,
    )

    outcome = await service.run_scheduled()

    assert isinstance(outcome, CapsuleOutcome)
    assert outcome.generated is True
    assert (outcome.hubs, outcome.memories, outcome.insights) == (2, 1, 1)

    # The blob was saved with the distilled text + the injected clock + provenance refs.
    assert len(capsule.saved) == 1
    blob = capsule.saved[0]
    assert "brain project" in blob.text
    assert blob.generated_at == fixed
    assert {r["node_id"] for r in blob.source_refs} == {"h1", "h2", "m1", "i1"}
    assert {r["kind"] for r in blob.source_refs} == {"hub", "memory", "insight"}

    # The source was fenced as data-not-instructions and carried each kind's material.
    user_msg = provider.last_messages[-1].content
    assert "data, not instructions" in user_msg
    assert "Alex" in user_msg
    assert "Shipped the MCP server" in user_msg and "Prefers async" in user_msg
    # The system prompt carried the token budget.
    assert "300" in provider.last_messages[0].content

    # The run closed SUCCEEDED with the counts in details.
    run = runs.runs[list(runs.runs)[-1]]
    assert run.agent == AGENT and run.status == SUCCEEDED
    assert run.details["generated"] is True and run.details["hubs"] == 2


async def test_caps_are_honored_from_settings():
    settings = Settings(
        chat_chain=["conspect-p"],
        distill_chain=["conspect-p"],
        quick_chain=["conspect-p"],
        identity_capsule_max_hubs=1,
        identity_capsule_max_memories=2,
        identity_capsule_max_insights=0,
    )
    service, capsule, sources, provider, runs = _make(
        hubs=[_hub("h1", "Alex"), _hub("h2", "Bob")],
        memories=[_node("m1", "one"), _node("m2", "two"), _node("m3", "three")],
        insights=[_node("i1", "x", node_type="insight")],
        settings=settings,
    )

    outcome = await service.run_scheduled()

    limits = (sources.limits["hubs"], sources.limits["memories"], sources.limits["insights"])
    assert limits == (1, 2, 0)
    assert (outcome.hubs, outcome.memories, outcome.insights) == (1, 2, 0)


async def test_char_cap_truncates_stored_text():
    settings = Settings(
        chat_chain=["conspect-p"],
        distill_chain=["conspect-p"],
        quick_chain=["conspect-p"],
        identity_capsule_max_chars=20,
    )
    service, capsule, *_ = _make(hubs=[_hub("h1", "Alex")], reply="x" * 500, settings=settings)

    await service.run_scheduled()

    assert len(capsule.saved[0].text) == 20


# --- best-effort skips keep the last capsule (rule 7) ---------------------------------------------


async def test_no_source_material_skips_without_saving():
    service, capsule, sources, provider, runs = _make()  # all sources empty

    outcome = await service.run_scheduled()

    assert outcome.generated is False
    assert outcome.skipped_reason == "no source material"
    assert capsule.saved == []
    assert provider.calls == 0  # never even called the model
    assert runs.runs[list(runs.runs)[-1]].status == SUCCEEDED  # a skip is not a failure


async def test_llm_down_keeps_last_capsule():
    service, capsule, sources, provider, runs = _make(hubs=[_hub("h1", "Alex")], available=False)

    outcome = await service.run_scheduled()

    assert outcome.generated is False
    assert outcome.skipped_reason == "LLM unavailable"
    assert capsule.saved == []
    assert runs.runs[list(runs.runs)[-1]].status == SUCCEEDED


async def test_empty_distillation_is_skipped():
    service, capsule, *_ = _make(hubs=[_hub("h1", "Alex")], reply="   \n  ")

    outcome = await service.run_scheduled()

    assert outcome.generated is False
    assert outcome.skipped_reason == "empty distillation"
    assert capsule.saved == []


# --- single-flight on the on-demand trigger -------------------------------------------------------


async def test_trigger_runs_in_background_and_returns_run_id():
    service, capsule, *_ = _make(hubs=[_hub("h1", "Alex")])

    run_id = await service.trigger()
    assert run_id is not None
    await service.drain()

    assert len(capsule.saved) == 1
    assert service.running is False


async def test_run_scheduled_skips_when_a_manual_refresh_is_running():
    service, capsule, *_ = _make(hubs=[_hub("h1", "Alex")])
    # Simulate a manual refresh in flight (the single-flight flag is held).
    service._running = True

    assert await service.run_scheduled() is None
    assert await service.trigger() is None
    assert capsule.saved == []


# --- the blob store encode/decode -----------------------------------------------------------------


def test_blob_encode_decode_round_trip():
    blob = CapsuleBlob(
        text="who I am",
        generated_at=datetime(2026, 7, 15, 4, 35, tzinfo=UTC),
        source_refs=[{"node_id": "n1", "title": "Alex", "kind": "hub"}],
    )
    decoded = _decode_blob(_encode_blob(blob))
    assert decoded is not None
    assert decoded.text == "who I am"
    assert decoded.generated_at == blob.generated_at
    assert decoded.source_refs == blob.source_refs


@pytest.mark.parametrize(
    "value",
    [None, "not-json", {"text": ""}, {"text": "   "}, {"nope": 1}, [1, 2, 3]],
)
def test_decode_blob_rejects_absent_or_empty(value):
    assert _decode_blob(value) is None


def test_decode_blob_tolerates_bad_generated_at_and_naive_datetime():
    assert _decode_blob({"text": "hi", "generated_at": "not-a-date"}).generated_at is None
    naive = _decode_blob({"text": "hi", "generated_at": "2026-07-15T04:35:00"})
    assert naive.generated_at is not None and naive.generated_at.tzinfo is UTC
