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
    claude = FakeChatProvider("claude-max", reply="from claude")
    nebius = FakeChatProvider("nebius", reply="from nebius")
    reg = _registry([claude, nebius], chat_chain=["claude-max", "nebius"])

    result = await reg.chat(MESSAGES)

    assert result.text == "from claude"
    assert result.model_used == "claude-max"
    assert result.fallback_used is False
    assert nebius.calls == 0  # fallback never touched


async def test_claude_limit_falls_back_to_nebius_and_records_it():
    claude = FakeChatProvider("claude-max", available=False)  # simulate usage-limit / down
    nebius = FakeChatProvider("nebius", reply="from nebius")
    reg = _registry([claude, nebius], chat_chain=["claude-max", "nebius"])

    result = await reg.chat(MESSAGES)

    assert result.text == "from nebius"
    assert result.model_used == "nebius"
    assert result.fallback_used is True
    assert claude.calls == 1 and nebius.calls == 1


async def test_all_providers_down_raises_exhausted():
    claude = FakeChatProvider("claude-max", available=False)
    nebius = FakeChatProvider("nebius", available=False)
    reg = _registry([claude, nebius], chat_chain=["claude-max", "nebius"])

    with pytest.raises(RegistryExhausted):
        await reg.chat(MESSAGES)


async def test_requested_model_is_tried_first():
    claude = FakeChatProvider("claude-max", reply="from claude")
    nebius = FakeChatProvider("nebius", reply="from nebius")
    reg = _registry([claude, nebius], chat_chain=["claude-max", "nebius"])

    result = await reg.chat(MESSAGES, requested_model="nebius")

    assert result.model_used == "nebius"
    assert result.fallback_used is False  # requested provider is the head of the chain
    assert claude.calls == 0


async def test_requested_model_still_falls_back_when_it_is_down():
    claude = FakeChatProvider("claude-max", reply="from claude")
    nebius = FakeChatProvider("nebius", available=False)
    reg = _registry([claude, nebius], chat_chain=["claude-max", "nebius"])

    # User asked for nebius; it's down → chain continues to the configured claude-max.
    result = await reg.chat(MESSAGES, requested_model="nebius")

    assert result.model_used == "claude-max"
    assert result.fallback_used is True


async def test_distill_uses_its_own_chain():
    claude = FakeChatProvider("claude-max", available=False)
    nebius = FakeChatProvider("nebius", reply="distilled")
    reg = _registry(
        [claude, nebius],
        chat_chain=["claude-max"],
        distill_chain=["claude-max", "nebius"],
    )

    result = await reg.distill(MESSAGES)

    assert result.text == "distilled"
    assert result.fallback_used is True


async def test_available_and_default_chat_models():
    claude = FakeChatProvider("claude-max")
    nebius = FakeChatProvider("nebius")
    reg = _registry([claude, nebius], chat_chain=["claude-max", "nebius"])

    assert reg.available_chat_models() == ["claude-max", "nebius"]
    assert reg.default_chat_model() == "claude-max"


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
