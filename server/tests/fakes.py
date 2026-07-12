"""Fake providers for service tests — no live LLMs in CI (08 testing policy)."""

from __future__ import annotations

from app.providers.base import (
    ChatMessage,
    ChatProvider,
    EmbeddingProvider,
    ProviderUnavailable,
)


class FakeChatProvider(ChatProvider):
    """A chat provider that either answers with a fixed reply or is always unavailable."""

    def __init__(self, id: str, *, reply: str | None = None, available: bool = True) -> None:
        self.id = id
        self._reply = reply if reply is not None else f"answer from {id}"
        self._available = available
        self.calls = 0

    async def health(self) -> bool:
        return self._available

    async def complete(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        self.calls += 1
        if not self._available:
            raise ProviderUnavailable(f"{self.id} is down")
        return self._reply


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, id: str = "fake-embed", dim: int = 8) -> None:
        self.id = id
        self._dim = dim

    async def health(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] * self._dim for t in texts]
