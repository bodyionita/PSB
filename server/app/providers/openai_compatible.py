"""One client for every OpenAI-compatible endpoint (ADR-004).

Serves OpenAI itself (embeddings + Whisper STT) and Nebius (chat). A new compatible
provider is config-only — no new code.
"""

from __future__ import annotations

import httpx

from .base import (
    ChatMessage,
    ChatProvider,
    EmbeddingProvider,
    ProviderUnavailable,
    STTProvider,
)

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


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
        embedding_model: str = "",
        stt_model: str = "",
    ) -> None:
        self.id = id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._default_chat_model = default_chat_model
        self._embedding_model = embedding_model
        self._stt_model = stt_model

    async def health(self) -> bool:
        # Cheap proxy — a configured key. Real failures surface as ProviderUnavailable and
        # drive fallback. /health never calls this (it must not touch an LLM).
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def complete(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        if not self._api_key:
            raise ProviderUnavailable(f"{self.id}: no API key configured")
        payload = {
            "model": model or self._default_chat_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
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
        if not self._api_key:
            raise ProviderUnavailable(f"{self.id}: no API key configured")
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
        if not self._api_key:
            raise ProviderUnavailable(f"{self.id}: no API key configured")
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
