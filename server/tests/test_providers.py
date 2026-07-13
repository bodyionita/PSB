"""OpenAI-compatible provider auth modes + registry embedding wiring (ADR-022).

The Ollama embeddings sidecar is keyless (localhost): a provider constructed with
``requires_api_key=False`` must be considered available without a key and must not send an
Authorization header. Keyed providers (OpenAI/Nebius/Groq) keep the old behavior.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.providers.base import EmbeddingProvider, ProviderUnavailable
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import build_registry


def _keyless() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        id="ollama",
        base_url="http://localhost:11434/v1",
        api_key="",
        embedding_model="nomic-embed-text",
        requires_api_key=False,
    )


async def test_keyless_provider_is_available_without_a_key():
    provider = _keyless()
    assert await provider.health() is True
    # No key ⇒ no Authorization header (rather than an empty Bearer token).
    assert provider._headers() == {}
    # The key guard is skipped: no "no API key configured" ProviderUnavailable.
    provider._require_available()  # must not raise


async def test_keyed_provider_without_key_is_unavailable():
    provider = OpenAICompatibleProvider(
        id="openai", base_url="https://api.openai.com/v1", api_key=""
    )
    assert await provider.health() is False
    with pytest.raises(ProviderUnavailable):
        provider._require_available()


async def test_keyed_provider_with_key_sends_bearer_header():
    provider = OpenAICompatibleProvider(
        id="nebius", base_url="https://api.studio.nebius.ai/v1", api_key="sk-test"
    )
    assert await provider.health() is True
    assert provider._headers() == {"Authorization": "Bearer sk-test"}


def test_build_registry_wires_ollama_as_the_embedding_provider():
    reg = build_registry(Settings())
    assert reg._embedding_provider_id == "ollama"
    ollama = reg._providers["ollama"]
    assert isinstance(ollama, EmbeddingProvider)
    # Embeddings left OpenAI entirely (ADR-022): OpenAI stays only as an STT provider.
    assert reg._embedding_provider_id != "openai"
