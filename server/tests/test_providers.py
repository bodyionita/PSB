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
from app.providers.base import ChatMessage, ChatProvider, EmbeddingProvider, ProviderUnavailable
from app.providers.claude import ClaudeProvider
from app.providers.openai_compatible import OpenAICompatibleProvider, _render_messages
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


# --- vision: image_url content parts + N-models-per-provider (M9, ADR-057 §4) -------------------
def test_render_messages_without_images_keeps_plain_string_content():
    msgs = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]
    rendered = _render_messages(msgs, None)
    assert rendered == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_render_messages_attaches_image_url_parts_to_last_user_message():
    msgs = [ChatMessage(role="system", content="describe"), ChatMessage(role="user", content="go")]
    rendered = _render_messages(msgs, ["data:image/png;base64,AAA", "data:image/jpeg;base64,BBB"])
    # System message untouched; the user message becomes a multimodal parts list (text first).
    assert rendered[0] == {"role": "system", "content": "describe"}
    assert rendered[1]["role"] == "user"
    assert rendered[1]["content"] == [
        {"type": "text", "text": "go"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBB"}},
    ]


def test_render_messages_drops_blank_image_urls():
    # `images` is untrusted at the provider boundary: a blank url must not become an empty part.
    msgs = [ChatMessage(role="user", content="go")]
    assert _render_messages(msgs, ["", None]) == [{"role": "user", "content": "go"}]  # type: ignore[list-item]
    rendered = _render_messages(msgs, ["", "data:image/png;base64,AAA"])
    assert rendered[0]["content"] == [
        {"type": "text", "text": "go"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]


async def test_complete_sends_image_parts_in_the_payload(monkeypatch):
    """A vision call builds the OpenAI `image_url` payload — captured at the HTTP boundary so a
    regression that dropped the image parts would show here."""
    provider = OpenAICompatibleProvider(
        id="groq", base_url="https://api.groq.com/openai/v1", api_key="k", default_chat_model="vlm"
    )
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "a cat"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *, headers, json):
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr("app.providers.openai_compatible.httpx.AsyncClient", _Client)
    out = await provider.complete(
        [ChatMessage(role="user", content="describe")],
        model="vlm",
        images=["data:image/png;base64,ZZZ"],
    )
    assert out == "a cat"
    content = captured["json"]["messages"][0]["content"]
    assert {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZZZ"}} in content


def test_openai_compatible_serves_extra_chat_models():
    # One endpoint may serve N chat models (ADR-045 / ADR-057 §4 — a text model + a VLM).
    provider = OpenAICompatibleProvider(
        id="nebius",
        base_url="x",
        api_key="k",
        default_chat_model="llama",
        extra_chat_models=("qwen-vl",),
    )
    assert provider.can_chat is True
    assert provider.chat_model_ids() == ("llama", "qwen-vl")


def test_build_registry_wires_the_vision_vlms_into_the_catalog():
    reg = build_registry(Settings())
    catalog = {m.id: m for m in reg.chat_models()}
    settings = Settings()
    # Groq's VLM is now a chat model served by `groq`; Nebius serves its text model AND the VLM.
    assert catalog[settings.groq_vision_model].provider == "groq"
    assert catalog[settings.nebius_vision_model].provider == "nebius"
    groq = reg._providers["groq"]
    assert isinstance(groq, ChatProvider) and groq.can_chat is True


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
    monkeypatch.setattr("app.providers.claude.subprocess.run", _fake_run_factory({}, stdout="   "))
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
