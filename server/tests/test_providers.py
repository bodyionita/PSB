"""OpenAI-compatible provider auth modes + registry embedding wiring (ADR-022).

The Ollama embeddings sidecar is keyless (localhost): a provider constructed with
``requires_api_key=False`` must be considered available without a key and must not send an
Authorization header. Keyed providers (OpenAI/Nebius/Groq) keep the old behavior.
"""

from __future__ import annotations

import subprocess

import pytest

from app.config import Settings
from app.providers.base import ChatMessage, EmbeddingProvider, ProviderUnavailable
from app.providers.claude_max import ClaudeMaxProvider
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


# --- claude-max CLI stdio (ADR-004 provider boundary + ADR-041 diacritics) --------------------
def _fake_run_factory(captured: dict, *, returncode: int = 0, stdout: str = "ok", stderr: str = ""):
    def _fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    return _fake_run


async def test_claude_max_pins_utf8_replace_on_cli_stdio(monkeypatch):
    """The CLI emits UTF-8: decode must be pinned so a non-UTF-8 host locale (cp1252 on Windows)
    can't mojibake organizer output before the ADR-041 fold, with errors=replace so a stray byte
    degrades (rule 7) rather than raising a UnicodeDecodeError past complete()'s narrow except."""
    provider = ClaudeMaxProvider(model="claude-opus-4-8")
    monkeypatch.setattr(provider, "_resolve_cli", lambda: "claude")
    captured: dict = {}
    monkeypatch.setattr(
        "app.providers.claude_max.subprocess.run", _fake_run_factory(captured, stdout="hello")
    )
    out = await provider.complete([ChatMessage(role="user", content="hi")])
    assert out == "hello"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


async def test_claude_max_nonzero_returncode_degrades_to_unavailable(monkeypatch):
    provider = ClaudeMaxProvider(model="m")
    monkeypatch.setattr(provider, "_resolve_cli", lambda: "claude")
    monkeypatch.setattr(
        "app.providers.claude_max.subprocess.run",
        _fake_run_factory({}, returncode=1, stderr="not logged in"),
    )
    with pytest.raises(ProviderUnavailable):
        await provider.complete([ChatMessage(role="user", content="hi")])


async def test_claude_max_empty_output_degrades_to_unavailable(monkeypatch):
    provider = ClaudeMaxProvider(model="m")
    monkeypatch.setattr(provider, "_resolve_cli", lambda: "claude")
    monkeypatch.setattr(
        "app.providers.claude_max.subprocess.run", _fake_run_factory({}, stdout="   ")
    )
    with pytest.raises(ProviderUnavailable):
        await provider.complete([ChatMessage(role="user", content="hi")])
