"""OpenAI-compatible provider auth modes + registry embedding wiring (ADR-022).

The Ollama embeddings sidecar is keyless (localhost): a provider constructed with
``requires_api_key=False`` must be considered available without a key and must not send an
Authorization header. Keyed providers (OpenAI/Nebius/Groq) keep the old behavior.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.config import Settings
from app.providers.base import ChatMessage, EmbeddingProvider, ProviderUnavailable
from app.providers.claude import ClaudeProvider
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


# --- claude CLI stdio (ADR-004 provider boundary + ADR-041 diacritics) --------------------
def _fake_run_factory(captured: dict, *, returncode: int = 0, stdout: str = "ok", stderr: str = ""):
    def _fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    return _fake_run


async def test_claude_pins_utf8_replace_on_cli_stdio(monkeypatch):
    """The CLI emits UTF-8: decode must be pinned so a non-UTF-8 host locale (cp1252 on Windows)
    can't mojibake organizer output before the ADR-041 fold, with errors=replace so a stray byte
    degrades (rule 7) rather than raising a UnicodeDecodeError past complete()'s narrow except."""
    provider = ClaudeProvider(models=["claude-opus-4-8"])
    monkeypatch.setattr(provider, "_resolve_cli", lambda: "claude")
    captured: dict = {}
    monkeypatch.setattr(
        "app.providers.claude.subprocess.run", _fake_run_factory(captured, stdout="hello")
    )
    out = await provider.complete([ChatMessage(role="user", content="hi")])
    assert out == "hello"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


async def test_claude_nonzero_returncode_degrades_to_unavailable(monkeypatch):
    provider = ClaudeProvider(models=["m"])
    monkeypatch.setattr(provider, "_resolve_cli", lambda: "claude")
    monkeypatch.setattr(
        "app.providers.claude.subprocess.run",
        _fake_run_factory({}, returncode=1, stderr="not logged in"),
    )
    with pytest.raises(ProviderUnavailable):
        await provider.complete([ChatMessage(role="user", content="hi")])


async def test_claude_empty_output_degrades_to_unavailable(monkeypatch):
    provider = ClaudeProvider(models=["m"])
    monkeypatch.setattr(provider, "_resolve_cli", lambda: "claude")
    monkeypatch.setattr(
        "app.providers.claude.subprocess.run", _fake_run_factory({}, stdout="   ")
    )
    with pytest.raises(ProviderUnavailable):
        await provider.complete([ChatMessage(role="user", content="hi")])


async def test_claude_invalid_utf8_byte_decodes_to_replacement_end_to_end(monkeypatch):
    """End-to-end decode (not just the kwargs boundary): a stray invalid byte from the CLI is
    decoded via the pinned ``errors='replace'`` → U+FFFD and ``complete()`` returns the cleaned
    text, degrading instead of raising a ``UnicodeDecodeError`` past its narrow ``except`` (rule 7).
    Drives a REAL subprocess emitting a real ``0xFF`` byte, honouring the provider's actual
    encoding/errors kwargs — so a regression that dropped ``errors='replace'`` would raise here."""
    real_run = subprocess.run

    def _run_real_invalid_bytes(args, **kwargs):
        # The provider must pin utf-8 + replace; forward those exact kwargs onto a real subprocess
        # that writes an invalid byte, so the genuine stdio decode path turns it into U+FFFD.
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"
        emitter = [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'Hi \xff there')"]
        return real_run(
            emitter,
            capture_output=kwargs.get("capture_output", True),
            text=kwargs.get("text", True),
            encoding=kwargs["encoding"],
            errors=kwargs["errors"],
            timeout=kwargs.get("timeout", 30),
        )

    provider = ClaudeProvider(models=["m"])
    monkeypatch.setattr(provider, "_resolve_cli", lambda: "claude")
    monkeypatch.setattr("app.providers.claude.subprocess.run", _run_real_invalid_bytes)
    out = await provider.complete([ChatMessage(role="user", content="hi")])
    assert "�" in out  # the 0xFF became the replacement char — decoded, not crashed
    assert out.startswith("Hi") and out.endswith("there")
