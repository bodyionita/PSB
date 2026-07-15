"""ModelRoutingService tests — the UI-editable routing brain (ADR-025 / ADR-043 / ADR-045).

Verifies the three groups resolve from config seeds (chains of MODEL ids), saved overrides win,
per-model effort is threaded only to effort-capable models, and a bad saved model id degrades to
the seed chain (rule 7). Fakes only: no live LLM/DB (08 testing policy).
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.providers.base import ChatMessage
from app.providers.registry import ProviderRegistry, RegistryExhausted
from app.services.model_routing import GroupRouting, ModelRoutingService

from .fakes import FakeChatProvider, FakeModelRoutingStore

MESSAGES = [ChatMessage(role="user", content="hi")]


def _registry(
    **overrides: FakeChatProvider,
) -> tuple[ProviderRegistry, dict[str, FakeChatProvider]]:
    """A registry over effort-capable Claude models + a non-effort fallback, keyed by MODEL id
    (ADR-045 — a routing group's active/fallback are model ids). Each fake is a one-model provider
    here (the one-provider-N-models case is covered in test_registry_fallback)."""
    providers = {
        "claude-opus-4-8": FakeChatProvider("claude-opus-4-8", reply="opus", supports_effort=True),
        "claude-sonnet-4-6": FakeChatProvider(
            "claude-sonnet-4-6", reply="sonnet", supports_effort=True
        ),
        "nebius": FakeChatProvider("nebius", reply="nebius"),
    }
    providers.update(overrides)
    reg = ProviderRegistry(
        providers,
        chat_chain=["claude-opus-4-8", "nebius"],
        distill_chain=["claude-opus-4-8", "nebius"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    return reg, providers


def _settings() -> Settings:
    # Explicit chains of model ids keyed by the fakes; effort seed is the config default (medium)
    # for every group (ADR-045 §5 — one `claude_effort` scalar, no per-tier effort).
    return Settings(
        chat_chain=["claude-opus-4-8", "nebius"],
        distill_chain=["claude-opus-4-8", "nebius"],
        quick_chain=["claude-sonnet-4-6", "nebius"],
    )


def _service(
    reg: ProviderRegistry, *, saved: dict[str, GroupRouting] | None = None
) -> tuple[ModelRoutingService, FakeModelRoutingStore]:
    store = FakeModelRoutingStore(saved)
    return ModelRoutingService(settings=_settings(), store=store, registry=reg), store


# --- seed resolution (no saved overrides) -------------------------------------------------------


async def test_conspect_seed_from_config():
    reg, _ = _registry()
    service, _ = _service(reg)

    decision = await service.resolve("conspect")

    assert decision.chain == ["claude-opus-4-8", "nebius"]
    # Only the effort-capable model carries the seed effort (claude_effort default = medium).
    assert decision.effort_by_model == {"claude-opus-4-8": "medium"}


async def test_quick_seed_is_sonnet_at_claude_effort():
    reg, _ = _registry()
    service, _ = _service(reg)

    decision = await service.resolve("quick")

    assert decision.chain == ["claude-sonnet-4-6", "nebius"]
    # ADR-045 §5 collapsed the per-tier effort scalars into one `claude_effort` (medium): the quick
    # tier's cheapness now comes from the Sonnet MODEL, not a lower effort seed. The user can still
    # tune quick to a lower effort in Settings → Models.
    assert decision.effort_by_model == {"claude-sonnet-4-6": "medium"}


async def test_groups_are_independent():
    reg, _ = _registry()
    service, _ = _service(reg)

    assert (await service.resolve("chat")).chain == ["claude-opus-4-8", "nebius"]
    assert (await service.resolve("quick")).chain[0] == "claude-sonnet-4-6"


async def test_unknown_group_raises():
    reg, _ = _registry()
    service, _ = _service(reg)
    with pytest.raises(ValueError):
        await service.resolve("nonsense")


# --- completion threads chain + effort ----------------------------------------------------------


async def test_complete_routes_conspect_to_primary():
    reg, providers = _registry()
    service, _ = _service(reg)

    result = await service.complete("conspect", MESSAGES)

    assert result.text == "opus"
    assert result.model_used == "claude-opus-4-8"
    assert result.fallback_used is False
    assert providers["nebius"].calls == 0
    # Seed effort reached the effort-capable primary; nebius was never asked.
    assert providers["claude-opus-4-8"].efforts == ["medium"]


async def test_complete_falls_back_and_records():
    down = FakeChatProvider("claude-opus-4-8", available=False, supports_effort=True)
    reg, providers = _registry(**{"claude-opus-4-8": down})
    service, _ = _service(reg)

    result = await service.complete("conspect", MESSAGES)

    assert result.text == "nebius"
    assert result.model_used == "nebius"
    assert result.fallback_used is True
    # Effort-incapable fallback was invoked with no effort value.
    assert providers["nebius"].efforts == [None]


async def test_all_down_raises_exhausted():
    reg, _ = _registry(
        **{
            "claude-opus-4-8": FakeChatProvider("claude-opus-4-8", available=False),
            "nebius": FakeChatProvider("nebius", available=False),
        }
    )
    service, _ = _service(reg)
    with pytest.raises(RegistryExhausted):
        await service.complete("conspect", MESSAGES)


async def test_requested_model_tried_first():
    reg, providers = _registry()
    service, _ = _service(reg)

    result = await service.complete("chat", MESSAGES, requested_model="nebius")

    assert result.model_used == "nebius"
    assert result.fallback_used is False
    assert providers["claude-opus-4-8"].calls == 0


# --- saved overrides + rule-7 degradation -------------------------------------------------------


async def test_saved_override_wins_over_seed():
    reg, providers = _registry()
    saved = {
        "chat": GroupRouting(
            active="nebius", fallback="claude-opus-4-8", effort_by_model={"claude-opus-4-8": "high"}
        )
    }
    service, _ = _service(reg, saved=saved)

    decision = await service.resolve("chat")
    assert decision.chain == ["nebius", "claude-opus-4-8"]
    # Saved effort is kept for the effort-capable provider even when it's the fallback.
    assert decision.effort_by_model == {"claude-opus-4-8": "high"}

    result = await service.complete("chat", MESSAGES)
    assert result.model_used == "nebius"
    assert providers["claude-opus-4-8"].calls == 0


async def test_saved_high_effort_reaches_claude():
    reg, providers = _registry()
    saved = {
        "chat": GroupRouting(
            active="claude-opus-4-8", fallback="nebius", effort_by_model={"claude-opus-4-8": "high"}
        )
    }
    service, _ = _service(reg, saved=saved)

    await service.complete("chat", MESSAGES)

    assert providers["claude-opus-4-8"].efforts == ["high"]


async def test_effort_for_incapable_provider_is_dropped():
    reg, _ = _registry()
    saved = {
        "chat": GroupRouting(
            active="claude-opus-4-8", fallback="nebius", effort_by_model={"nebius": "high"}
        )
    }
    service, _ = _service(reg, saved=saved)

    decision = await service.resolve("chat")

    # Nebius has no reasoning-effort control, so a saved value for it is filtered out.
    assert decision.effort_by_model == {}


async def test_unknown_saved_model_degrades_to_seed():
    reg, _ = _registry()
    saved = {"chat": GroupRouting(active="ghost", fallback="phantom")}
    service, _ = _service(reg, saved=saved)

    decision = await service.resolve("chat")

    # Both saved ids are unknown → fall back to the config seed chain, never an empty/hard failure.
    assert decision.chain == ["claude-opus-4-8", "nebius"]


# --- cache busting on save ----------------------------------------------------------------------


async def test_save_busts_cache():
    reg, _ = _registry()
    service, store = _service(reg)

    # Prime the cache with the seed.
    assert (await service.resolve("chat")).chain == ["claude-opus-4-8", "nebius"]

    await service.save("chat", GroupRouting(active="nebius", fallback="claude-opus-4-8"))

    # The next resolve reflects the save without a restart (bust-on-save, ADR-025 §3).
    assert (await service.resolve("chat")).chain == ["nebius", "claude-opus-4-8"]
    assert store.saved["chat"].active == "nebius"
