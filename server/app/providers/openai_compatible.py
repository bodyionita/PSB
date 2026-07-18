"""One client for every OpenAI-compatible endpoint (ADR-004).

Serves OpenAI (Whisper STT fallback), Nebius (chat), Groq (STT primary), and the on-box
Ollama embeddings sidecar (ADR-022, keyless localhost). A new compatible provider is
config-only — no new code.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from .base import (
    ChatMessage,
    ChatProvider,
    EmbeddingProvider,
    ProviderUnavailable,
    STTProvider,
)

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _dedup(values: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving dedup of truthy strings."""
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return tuple(out)


def _render_messages(
    messages: list[ChatMessage], images: list[str] | None
) -> list[dict[str, object]]:
    """Render messages to the OpenAI chat-completions shape, attaching any ``images`` as
    ``image_url`` content parts on the LAST user message (M9, ADR-057 §4). Without images every
    message keeps a plain string ``content`` (unchanged text path); with images the target message's
    content becomes the multimodal parts list ``[{text}, {image_url}, …]`` an OpenAI-compatible VLM
    expects. If there is no user message (defensive), the parts attach to the last message."""
    rendered: list[dict[str, object]] = [{"role": m.role, "content": m.content} for m in messages]
    if not images or not rendered:
        return rendered
    target = next(
        (i for i in range(len(rendered) - 1, -1, -1) if rendered[i]["role"] == "user"),
        len(rendered) - 1,
    )
    text = rendered[target]["content"]
    rendered[target]["content"] = [
        {"type": "text", "text": text},
        *({"type": "image_url", "image_url": {"url": url}} for url in images),
    ]
    return rendered


class OpenAICompatibleProvider(ChatProvider, EmbeddingProvider, STTProvider):
    """Chat + embeddings + STT over the OpenAI HTTP shape.

    Which capabilities are *used* is decided by the registry's task routing, not here.
    """

    def __init__(
        self,
        *,
        id: str,
        base_url: str,
        api_key: str,
        default_chat_model: str = "",
        extra_chat_models: tuple[str, ...] = (),
        embedding_model: str = "",
        stt_model: str = "",
        provider_label: str = "",
        requires_api_key: bool = True,
    ) -> None:
        self.id = id
        # Friendly PROVIDER name for the ADR-044 Providers card (one row per provider — ADR-045 §6).
        # Model display names are derived per model id by the registry (labels.py), not here.
        self.provider_label = provider_label or id
        # This class also backs the STT/embedding providers (ADR-004); only an instance with a
        # configured chat model can actually chat, so it's excluded from GET /chat/models otherwise
        # (base.ChatProvider.can_chat). Non-chat instances (STT/embedding) pass no chat model.
        self.can_chat = bool(default_chat_model)
        # One OpenAI-compatible endpoint may serve N chat models (ADR-045 — like the `claude`
        # provider serving Opus+Sonnet): the `default` plus any extras (M9, ADR-057 §4 — a `vision`
        # VLM alongside the text chat model). The registry passes the resolved model id per call, so
        # one client reaches every model on the endpoint. Order-preserving dedup, truthy-only.
        self._chat_models = _dedup(m for m in (default_chat_model, *extra_chat_models) if m)
        # Parallel to ``can_chat`` — an instance only transcribes/embeds if configured with the
        # matching model, so the ADR-044 ``capabilities`` row reflects configuration (openai/groq =
        # stt-only, ollama = embedding-only, nebius = chat-only), not the all-capabilities class.
        self.can_transcribe = bool(stt_model)
        self.can_embed = bool(embedding_model)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._default_chat_model = default_chat_model
        self._embedding_model = embedding_model
        self._stt_model = stt_model
        # Localhost endpoints (the Ollama embeddings sidecar, ADR-022) authenticate implicitly
        # by network reachability — no key. When False, the key guard + Authorization header are
        # skipped; availability is reachability, not credentials.
        self._requires_api_key = requires_api_key

    def chat_model_ids(self) -> tuple[str, ...]:
        # One OpenAI-compatible endpoint may serve N chat models (ADR-045 / ADR-057 §4 — a text
        # model plus a VLM). Non-chat instances (STT/embedding) serve none, so they never enter the
        # chat catalog.
        return tuple(self._chat_models) if self.can_chat else ()

    async def health(self) -> bool:
        # Cheap proxy — a configured key (or none required for a localhost provider). Real
        # failures surface as ProviderUnavailable and drive fallback. /health never calls this
        # (it must not touch an LLM).
        return not self._requires_api_key or bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        # Omit auth entirely when no key is configured (keyless localhost), rather than send an
        # empty Bearer token.
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    def _require_available(self) -> None:
        if self._requires_api_key and not self._api_key:
            raise ProviderUnavailable(f"{self.id}: no API key configured")

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        effort: str | None = None,
        images: list[str] | None = None,
    ) -> str:
        # ``effort`` is accepted for interface parity (ADR-025 §4) but ignored: OpenAI-compatible
        # chat models (Nebius/Groq) have no reasoning-effort control (``supports_effort`` stays
        # False). ``images`` (M9, ADR-057 §4) are attached to the last user message as `image_url`
        # content parts for a `vision`-group VLM call; a plain text call passes none.
        self._require_available()
        payload = {
            "model": model or self._default_chat_model,
            "messages": _render_messages(messages, images),
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise ProviderUnavailable(f"{self.id} chat failed: {exc}") from exc

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._require_available()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._base_url}/embeddings",
                    headers=self._headers(),
                    json={"model": self._embedding_model, "input": texts},
                )
                resp.raise_for_status()
                data = resp.json()
            return [item["embedding"] for item in data["data"]]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderUnavailable(f"{self.id} embeddings failed: {exc}") from exc

    async def transcribe(self, audio: bytes, *, filename: str) -> str:
        self._require_available()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._base_url}/audio/transcriptions",
                    headers=self._headers(),
                    data={"model": self._stt_model},
                    files={"file": (filename, audio)},
                )
                resp.raise_for_status()
                data = resp.json()
            return data["text"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderUnavailable(f"{self.id} transcription failed: {exc}") from exc
