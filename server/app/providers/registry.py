"""Provider registry + fallback routing (ADR-004).

Config declares named providers and per-task chains. The registry walks a chain, advancing
past any provider that raises :class:`ProviderUnavailable`, and reports which model actually
answered (``model_used``) and whether a fallback fired (``fallback_used``) — the resolution
is never swallowed (CLAUDE.md rule 3).
"""

from __future__ import annotations

import logging

from ..config import Settings
from .base import (
    ChatMessage,
    ChatProvider,
    ChatResult,
    EmbeddingProvider,
    EmbeddingResult,
    Provider,
    ProviderUnavailable,
    STTProvider,
    TranscriptResult,
)
from .claude_max import ClaudeMaxProvider
from .openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


class RegistryExhausted(ProviderUnavailable):
    """Every provider in a chain was unavailable."""


class ProviderRegistry:
    def __init__(
        self,
        providers: dict[str, Provider],
        *,
        chat_chain: list[str],
        distill_chain: list[str],
        embedding_provider_id: str,
        stt_chain: list[str],
    ) -> None:
        self._providers = providers
        self._chat_chain = chat_chain
        self._distill_chain = distill_chain
        self._embedding_provider_id = embedding_provider_id
        self._stt_chain = stt_chain

    # --- introspection (feeds GET /chat/models & GET /settings) ---
    def available_chat_models(self) -> list[str]:
        return [pid for pid in self._chat_chain if pid in self._providers]

    def default_chat_model(self) -> str:
        chain = self.available_chat_models()
        return chain[0] if chain else ""

    def _resolve_chain(self, requested: str | None, default_chain: list[str]) -> list[str]:
        """A requested provider is tried first, then the remaining configured fallbacks."""
        if requested and requested in self._providers:
            rest = [pid for pid in default_chain if pid != requested]
            return [requested, *rest]
        return list(default_chain)

    async def _chat_over_chain(self, messages: list[ChatMessage], chain: list[str]) -> ChatResult:
        errors: list[str] = []
        for index, provider_id in enumerate(chain):
            provider = self._providers.get(provider_id)
            if not isinstance(provider, ChatProvider):
                errors.append(f"{provider_id}: not a chat provider")
                continue
            try:
                text = await provider.complete(messages)
            except ProviderUnavailable as exc:
                logger.warning("chat provider %s unavailable: %s", provider_id, exc)
                errors.append(str(exc))
                continue
            return ChatResult(text=text, model_used=provider_id, fallback_used=index > 0)
        raise RegistryExhausted("all chat providers unavailable: " + "; ".join(errors))

    async def chat(
        self, messages: list[ChatMessage], *, requested_model: str | None = None
    ) -> ChatResult:
        chain = self._resolve_chain(requested_model, self._chat_chain)
        return await self._chat_over_chain(messages, chain)

    async def distill(self, messages: list[ChatMessage]) -> ChatResult:
        """Agent/distillation path — configured separately from the chat picker (ADR-004)."""
        return await self._chat_over_chain(messages, list(self._distill_chain))

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        provider = self._providers.get(self._embedding_provider_id)
        if not isinstance(provider, EmbeddingProvider):
            raise ProviderUnavailable(
                f"embedding provider '{self._embedding_provider_id}' not registered"
            )
        vectors = await provider.embed(texts)
        return EmbeddingResult(vectors=vectors, model_used=self._embedding_provider_id)

    def available_stt_models(self) -> list[str]:
        return [pid for pid in self._stt_chain if pid in self._providers]

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
        """Walk the STT chain (ADR-020), advancing past any ProviderUnavailable (an OpenAI 429
        is one) and recording which provider answered + whether a fallback fired (rule 3)."""
        errors: list[str] = []
        for index, provider_id in enumerate(self._stt_chain):
            provider = self._providers.get(provider_id)
            if not isinstance(provider, STTProvider):
                errors.append(f"{provider_id}: not an STT provider")
                continue
            try:
                text = await provider.transcribe(audio, filename=filename)
            except ProviderUnavailable as exc:
                logger.warning("STT provider %s unavailable: %s", provider_id, exc)
                errors.append(str(exc))
                continue
            return TranscriptResult(text=text, model_used=provider_id, fallback_used=index > 0)
        raise RegistryExhausted("all STT providers unavailable: " + "; ".join(errors))


def build_registry(settings: Settings) -> ProviderRegistry:
    """Construct the registry from settings — the only place providers are instantiated."""
    openai = OpenAICompatibleProvider(
        id="openai",
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        embedding_model=settings.embedding_model,
        stt_model=settings.stt_model,
    )
    nebius = OpenAICompatibleProvider(
        id="nebius",
        base_url=settings.nebius_base_url,
        api_key=settings.nebius_api_key,
        default_chat_model=settings.nebius_chat_model,
    )
    # Groq — STT primary (ADR-020). Same OpenAI-compatible class, different endpoint/key/model.
    groq = OpenAICompatibleProvider(
        id="groq",
        base_url=settings.groq_base_url,
        api_key=settings.groq_api_key,
        stt_model=settings.groq_stt_model,
    )
    claude_max = ClaudeMaxProvider(
        id="claude-max",
        model=settings.claude_max_model,
        effort=settings.claude_max_effort,
    )

    providers: dict[str, Provider] = {
        "openai": openai,
        "nebius": nebius,
        "groq": groq,
        "claude-max": claude_max,
    }
    return ProviderRegistry(
        providers,
        chat_chain=settings.chat_chain,
        distill_chain=settings.distill_chain,
        embedding_provider_id="openai",
        stt_chain=settings.stt_chain,
    )
