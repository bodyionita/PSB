"""Provider registry + fallback routing (ADR-004).

Config declares named providers and per-task chains. The registry walks a chain, advancing
past any provider that raises :class:`ProviderUnavailable`, and reports which model actually
answered (``model_used``) and whether a fallback fired (``fallback_used``) — the resolution
is never swallowed (CLAUDE.md rule 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class ChatModelOption:
    """A pickable chat model for the composer/settings pickers (03-api §Chat/§Settings).

    ``GET /chat/models`` uses ``id``/``label``; ``GET /settings`` also renders ``supports_effort``
    and ``effort_levels`` so the effort selector appears only where it applies, with the levels
    registry-sourced (ADR-025 §6, no hardcoded enums in the web)."""

    id: str
    label: str
    supports_effort: bool = False
    effort_levels: list[str] = field(default_factory=list)


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
    def chat_models(self) -> list[ChatModelOption]:
        """Every genuinely chat-capable provider as ``{id, label}`` (registration order) — the
        pickable universe for the chat composer + Settings model dropdowns (03-api §Chat). Filters
        on ``can_chat`` (not merely the ``ChatProvider`` class), so the shared OpenAI-compatible
        STT/embedding instances are excluded. The label is provider-sourced (its configured model),
        falling back to the id."""
        return [
            ChatModelOption(
                id=pid,
                label=provider.label or pid,
                supports_effort=provider.supports_effort,
                effort_levels=list(provider.effort_levels),
            )
            for pid, provider in self._providers.items()
            if isinstance(provider, ChatProvider) and provider.can_chat
        ]

    def available_chat_models(self) -> list[str]:
        return [pid for pid in self._chat_chain if pid in self._providers]

    def default_chat_model(self) -> str:
        chain = self.available_chat_models()
        return chain[0] if chain else ""

    def supports_chat(self, provider_id: str) -> bool:
        """True if ``provider_id`` maps to a registered chat provider — the ModelRoutingService's
        rule-7 guard: an unknown/stale saved model id is filtered before a chain is walked."""
        return isinstance(self._providers.get(provider_id), ChatProvider)

    def supports_effort(self, provider_id: str) -> bool:
        """True if ``provider_id``'s chat provider honors a per-call ``effort`` (ADR-025 §4).
        Sourced from the provider, so the routing service + GET /settings never hardcode which
        models take effort."""
        provider = self._providers.get(provider_id)
        return isinstance(provider, ChatProvider) and provider.supports_effort

    def _resolve_chain(self, requested: str | None, default_chain: list[str]) -> list[str]:
        """A requested provider is tried first, then the remaining configured fallbacks."""
        if requested and requested in self._providers:
            rest = [pid for pid in default_chain if pid != requested]
            return [requested, *rest]
        return list(default_chain)

    async def _chat_over_chain(
        self,
        messages: list[ChatMessage],
        chain: list[str],
        effort_by_provider: dict[str, str] | None = None,
    ) -> ChatResult:
        efforts = effort_by_provider or {}
        errors: list[str] = []
        for index, provider_id in enumerate(chain):
            provider = self._providers.get(provider_id)
            if not isinstance(provider, ChatProvider):
                errors.append(f"{provider_id}: not a chat provider")
                continue
            try:
                # Per-provider effort (ADR-025 §4): only providers that support one get a value;
                # the rest receive None and use their construction default.
                text = await provider.complete(messages, effort=efforts.get(provider_id))
            except ProviderUnavailable as exc:
                logger.warning("chat provider %s unavailable: %s", provider_id, exc)
                errors.append(str(exc))
                continue
            return ChatResult(
                text=text,
                model_used=provider_id,
                fallback_used=index > 0,
                effort_used=efforts.get(provider_id),
            )
        raise RegistryExhausted("all chat providers unavailable: " + "; ".join(errors))

    async def run_chain(
        self,
        messages: list[ChatMessage],
        *,
        chain: list[str],
        effort_by_provider: dict[str, str] | None = None,
        requested_model: str | None = None,
    ) -> ChatResult:
        """Mechanics for the ModelRoutingService (ADR-025 §3): walk an explicit provider ``chain``
        (a requested model tried first), threading each provider's ``effort``, recording
        ``model_used``/``fallback_used``. The routing *brain* (which chain, what effort) lives in
        the service; the registry stays pure provider mechanics."""
        resolved = self._resolve_chain(requested_model, chain)
        return await self._chat_over_chain(messages, resolved, effort_by_provider)

    async def chat(
        self, messages: list[ChatMessage], *, requested_model: str | None = None
    ) -> ChatResult:
        return await self.run_chain(
            messages, chain=list(self._chat_chain), requested_model=requested_model
        )

    async def distill(self, messages: list[ChatMessage]) -> ChatResult:
        """Agent/distillation path — configured separately from the chat picker (ADR-004)."""
        return await self.run_chain(messages, chain=list(self._distill_chain))

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
    # OpenAI is STT fallback only now — embeddings moved to the self-hosted Ollama provider
    # (ADR-022), so no embedding_model here.
    openai = OpenAICompatibleProvider(
        id="openai",
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
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
    # A second ClaudeMaxProvider over the SAME `claude` CLI, driving a cheaper Sonnet tier for the
    # `quick` routing group (ADR-043 §3): one provider id = one configured model, so the chain /
    # requested_model / Settings machinery needs no new concept. The `quick` group supplies its
    # effort per call; the constructor effort is the no-per-call-effort default, seeded to the
    # cheap-tier `quick_effort` (low) to match the tier intent.
    claude_max_sonnet = ClaudeMaxProvider(
        id="claude-max-sonnet",
        model=settings.claude_max_sonnet_model,
        effort=settings.quick_effort,
    )
    # Self-hosted nomic embeddings (ADR-022): OpenAI-compatible /v1/embeddings on the on-box
    # Ollama sidecar, no API key (localhost). Single embedding provider — one index, one space.
    ollama = OpenAICompatibleProvider(
        id="ollama",
        base_url=settings.ollama_base_url,
        api_key="",
        embedding_model=settings.embedding_model,
        requires_api_key=False,
    )

    providers: dict[str, Provider] = {
        "openai": openai,
        "nebius": nebius,
        "groq": groq,
        "claude-max": claude_max,
        "claude-max-sonnet": claude_max_sonnet,
        "ollama": ollama,
    }
    return ProviderRegistry(
        providers,
        chat_chain=settings.chat_chain,
        distill_chain=settings.distill_chain,
        embedding_provider_id=settings.embedding_provider_id,
        stt_chain=settings.stt_chain,
    )
