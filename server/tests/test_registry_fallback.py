"""Provider fallback is the M0 acceptance centerpiece (ADR-012): a Claude-limit simulation
makes the chain answer via Nebius and records it. Verified here with fakes — no live LLMs.
"""

from __future__ import annotations

import pytest

from app.providers.base import ChatMessage, ProviderUnavailable
from app.providers.registry import ProviderRegistry, RegistryExhausted

from .fakes import FakeChatProvider, FakeEmbeddingProvider, FakeSTTProvider

MESSAGES = [ChatMessage(role="user", content="hello")]


def _registry(providers, *, chat_chain, distill_chain=None, embed_id="openai", stt_chain=None):
    return ProviderRegistry(
        {p.id: p for p in providers},
        chat_chain=chat_chain,
        distill_chain=distill_chain or chat_chain,
        embedding_provider_id=embed_id,
        stt_chain=stt_chain if stt_chain is not None else ["openai"],
    )


async def test_primary_answers_no_fallback():
    claude = FakeChatProvider("claude-opus-4-8", reply="from claude")
    nebius = FakeChatProvider("nebius", reply="from nebius")
    reg = _registry([claude, nebius], chat_chain=["claude-opus-4-8", "nebius"])

    result = await reg.chat(MESSAGES)

    assert result.text == "from claude"
    assert result.model_used == "claude-opus-4-8"
    assert result.fallback_used is False
    assert nebius.calls == 0  # fallback never touched


async def test_claude_limit_falls_back_to_nebius_and_records_it():
    claude = FakeChatProvider("claude-opus-4-8", available=False)  # simulate usage-limit / down
    nebius = FakeChatProvider("nebius", reply="from nebius")
    reg = _registry([claude, nebius], chat_chain=["claude-opus-4-8", "nebius"])

    result = await reg.chat(MESSAGES)

    assert result.text == "from nebius"
    assert result.model_used == "nebius"
    assert result.fallback_used is True
    assert claude.calls == 1 and nebius.calls == 1


async def test_all_providers_down_raises_exhausted():
    claude = FakeChatProvider("claude-opus-4-8", available=False)
    nebius = FakeChatProvider("nebius", available=False)
    reg = _registry([claude, nebius], chat_chain=["claude-opus-4-8", "nebius"])

    with pytest.raises(RegistryExhausted):
        await reg.chat(MESSAGES)


async def test_requested_model_is_tried_first():
    claude = FakeChatProvider("claude-opus-4-8", reply="from claude")
    nebius = FakeChatProvider("nebius", reply="from nebius")
    reg = _registry([claude, nebius], chat_chain=["claude-opus-4-8", "nebius"])

    result = await reg.chat(MESSAGES, requested_model="nebius")

    assert result.model_used == "nebius"
    assert result.fallback_used is False  # requested provider is the head of the chain
    assert claude.calls == 0


async def test_requested_model_still_falls_back_when_it_is_down():
    claude = FakeChatProvider("claude-opus-4-8", reply="from claude")
    nebius = FakeChatProvider("nebius", available=False)
    reg = _registry([claude, nebius], chat_chain=["claude-opus-4-8", "nebius"])

    # User asked for nebius; it's down → chain continues to the configured Claude model.
    result = await reg.chat(MESSAGES, requested_model="nebius")

    assert result.model_used == "claude-opus-4-8"
    assert result.fallback_used is True


async def test_distill_uses_its_own_chain():
    claude = FakeChatProvider("claude-opus-4-8", available=False)
    nebius = FakeChatProvider("nebius", reply="distilled")
    reg = _registry(
        [claude, nebius],
        chat_chain=["claude-opus-4-8"],
        distill_chain=["claude-opus-4-8", "nebius"],
    )

    result = await reg.distill(MESSAGES)

    assert result.text == "distilled"
    assert result.fallback_used is True


async def test_available_and_default_chat_models():
    claude = FakeChatProvider("claude-opus-4-8")
    nebius = FakeChatProvider("nebius")
    reg = _registry([claude, nebius], chat_chain=["claude-opus-4-8", "nebius"])

    assert reg.available_chat_models() == ["claude-opus-4-8", "nebius"]
    assert reg.default_chat_model() == "claude-opus-4-8"


def test_build_registry_claude_serves_opus_and_sonnet_as_models():
    """ADR-045: one `claude` provider serves BOTH Opus + Sonnet as model ids (the former two fake
    provider ids are gone); both honor per-call effort, the Nebius model does not."""
    from app.config import Settings
    from app.providers.registry import build_registry

    settings = Settings()
    reg = build_registry(settings)

    # Five providers (claude collapsed to one), and the two Claude models resolve to it.
    assert set(reg._providers) == {"openai", "nebius", "groq", "claude", "ollama"}
    assert reg._model_to_provider[settings.claude_opus_model].id == "claude"
    assert reg._model_to_provider[settings.claude_sonnet_model].id == "claude"

    # Both Claude models are pickable + effort-capable; the Nebius model is chat but effort-less.
    assert reg.supports_chat(settings.claude_opus_model)
    assert reg.supports_chat(settings.claude_sonnet_model)
    assert reg.supports_effort(settings.claude_opus_model)
    assert reg.supports_effort(settings.claude_sonnet_model)
    assert reg.supports_chat(settings.nebius_chat_model)
    assert not reg.supports_effort(settings.nebius_chat_model)

    # The catalog carries the provider per model + a friendly, model-derived label (labels.py).
    catalog = {m.id: m for m in reg.chat_models()}
    assert catalog[settings.claude_opus_model].provider == "claude"
    assert catalog[settings.claude_opus_model].label == "Claude Opus 4.8"
    assert catalog[settings.claude_sonnet_model].label == "Claude Sonnet 4.6"


async def test_build_registry_claude_routes_per_call_model(monkeypatch):
    """A chain of Claude model ids hits the ONE claude provider with the resolved `--model` per call
    (ADR-045): opus down → sonnet answers, both served by the same instance."""
    from app.config import Settings
    from app.providers.registry import build_registry

    settings = Settings()
    reg = build_registry(settings)
    claude = reg._providers["claude"]

    calls: list[str] = []

    async def _fake_complete(messages, *, model=None, effort=None, images=None):
        calls.append(model)
        if model == settings.claude_opus_model:
            raise ProviderUnavailable("opus down")
        return f"answered by {model}"

    monkeypatch.setattr(claude, "complete", _fake_complete)

    result = await reg.run_chain(
        MESSAGES, chain=[settings.claude_opus_model, settings.claude_sonnet_model]
    )
    assert result.model_used == settings.claude_sonnet_model
    assert result.fallback_used is True
    assert calls == [settings.claude_opus_model, settings.claude_sonnet_model]


async def test_embed_delegates_to_embedding_provider():
    embed = FakeEmbeddingProvider("openai", dim=4)
    reg = _registry([embed], chat_chain=[], embed_id="openai")

    result = await reg.embed(["ab", "abcd"])

    assert result.model_used == "openai"
    assert result.vectors == [[2.0, 2.0, 2.0, 2.0], [4.0, 4.0, 4.0, 4.0]]


async def test_embed_without_provider_raises():
    reg = _registry([], chat_chain=[], embed_id="missing")
    with pytest.raises(ProviderUnavailable):
        await reg.embed(["x"])


# --- STT fallback chain (ADR-020) --------------------------------------------------------


async def test_stt_primary_answers_no_fallback():
    groq = FakeSTTProvider("groq", transcript="from groq")
    openai = FakeSTTProvider("openai", transcript="from openai")
    reg = ProviderRegistry(
        {"groq": groq, "openai": openai},
        chat_chain=[],
        distill_chain=[],
        embedding_provider_id="none",
        stt_chain=["groq", "openai"],
    )

    result = await reg.transcribe(b"audio", filename="a.webm")

    assert result.text == "from groq"
    assert result.model_used == "groq"
    assert result.fallback_used is False
    assert openai.calls == 0  # fallback never touched


async def test_stt_falls_back_to_openai_on_groq_429_and_records_it():
    # A 429 surfaces as ProviderUnavailable → chain advances (ADR-020).
    groq = FakeSTTProvider("groq", available=False)
    openai = FakeSTTProvider("openai", transcript="from openai")
    reg = ProviderRegistry(
        {"groq": groq, "openai": openai},
        chat_chain=[],
        distill_chain=[],
        embedding_provider_id="none",
        stt_chain=["groq", "openai"],
    )

    result = await reg.transcribe(b"audio", filename="a.webm")

    assert result.text == "from openai"
    assert result.model_used == "openai"
    assert result.fallback_used is True
    assert groq.calls == 1 and openai.calls == 1


async def test_stt_all_providers_down_raises_exhausted():
    groq = FakeSTTProvider("groq", available=False)
    openai = FakeSTTProvider("openai", available=False)
    reg = ProviderRegistry(
        {"groq": groq, "openai": openai},
        chat_chain=[],
        distill_chain=[],
        embedding_provider_id="none",
        stt_chain=["groq", "openai"],
    )

    with pytest.raises(RegistryExhausted):
        await reg.transcribe(b"audio", filename="a.webm")
