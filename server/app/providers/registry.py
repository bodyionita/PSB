"""Provider registry + fallback routing (ADR-004 / ADR-045).

Config declares named providers and per-task chains **of model ids** (the raw vendor strings,
ADR-045). A provider serves N models; the registry keeps a **model→provider index** so a chain
resolves each model id to its provider instance + the vendor string to pass. It walks a chain,
advancing past any provider that raises :class:`ProviderUnavailable`, and reports which model
actually answered (``model_used``) and whether a fallback fired (``fallback_used``) — the
resolution is never swallowed (CLAUDE.md rule 3). Runtime status is keyed by **provider** id (a
fallback/error is a provider event — ADR-044/045 §6).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

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
from .claude import ClaudeProvider
from .labels import friendly_model_label
from .openai_compatible import OpenAICompatibleProvider
from .status import ProviderError, ProviderStatusTracker

logger = logging.getLogger(__name__)


class RegistryExhausted(ProviderUnavailable):
    """Every provider in a chain was unavailable."""


def _provider_capabilities(provider: Provider) -> list[str]:
    """The configured capabilities of a provider for the ADR-044 observability row — reflects
    configuration (``can_chat``/``can_transcribe``/``can_embed``), not merely the class hierarchy
    (the OpenAI-compatible class backs all three by type)."""
    caps: list[str] = []
    if isinstance(provider, ChatProvider) and provider.can_chat:
        caps.append("chat")
    if isinstance(provider, STTProvider) and provider.can_transcribe:
        caps.append("stt")
    if isinstance(provider, EmbeddingProvider) and provider.can_embed:
        caps.append("embedding")
    return caps


@dataclass(frozen=True)
class ProviderReport:
    """One provider's row for ``GET /admin/providers`` (ADR-044): identity + capabilities + a live
    reachability probe + the in-memory runtime status (sticky ``last_error``, ``last_success_at``,
    ``consecutive_failures``). ``reachable`` is config-reachability, **not** a success guarantee."""

    id: str
    label: str
    capabilities: list[str]
    reachable: bool
    last_error: ProviderError | None
    last_success_at: datetime | None
    consecutive_failures: int


@dataclass(frozen=True)
class ChatModelOption:
    """A pickable chat MODEL for the composer/settings pickers (03-api §Chat/§Settings, ADR-045).

    ``id`` is the raw vendor model string (the routable unit); ``provider`` is the id of the
    provider that serves it (derived, ADR-045 §1); ``label`` is the model-derived display name
    (labels.py). ``GET /settings`` also renders ``supports_effort`` + ``effort_levels`` so the
    effort selector appears only where it applies, levels registry-sourced (ADR-025 §6)."""

    id: str
    provider: str
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
        status_tracker: ProviderStatusTracker | None = None,
    ) -> None:
        self._providers = providers
        self._chat_chain = chat_chain
        self._distill_chain = distill_chain
        self._embedding_provider_id = embedding_provider_id
        self._stt_chain = stt_chain
        # In-memory per-PROVIDER runtime status (ADR-044/045 §6), recorded at every provider call
        # site keyed by provider id. Process-lifetime singleton on the registry (single uvicorn
        # worker), so consistent across requests; resets on redeploy (accepted — the failure mode
        # is persistent, it repopulates).
        self._status = status_tracker or ProviderStatusTracker()
        # Chat-model catalog + model→provider index (ADR-045): each chat-capable provider serves
        # N model ids; the catalog is the pickable universe and the index resolves a chain's model
        # id → its provider instance. Built once at construction, in registration order.
        self._chat_catalog: list[ChatModelOption] = []
        self._model_to_provider: dict[str, Provider] = {}
        for pid, provider in providers.items():
            if not (isinstance(provider, ChatProvider) and provider.can_chat):
                continue
            for model_id in provider.chat_model_ids():
                if not model_id or model_id in self._model_to_provider:
                    continue  # skip unconfigured / duplicate model ids
                self._chat_catalog.append(
                    ChatModelOption(
                        id=model_id,
                        provider=pid,
                        label=friendly_model_label(model_id),
                        supports_effort=provider.supports_effort,
                        effort_levels=list(provider.effort_levels),
                    )
                )
                self._model_to_provider[model_id] = provider

    # --- introspection (feeds GET /chat/models & GET /settings) ---
    def chat_models(self) -> list[ChatModelOption]:
        """Every pickable chat MODEL as ``{id, provider, label, …}`` (registration order) — the
        universe for the chat composer + Settings model dropdowns (03-api §Chat, ADR-045). Built
        from each chat-capable provider's ``chat_model_ids()`` (``can_chat`` filtered, so the shared
        OpenAI-compatible STT/embedding instances are excluded), the label derived per model id."""
        return list(self._chat_catalog)

    def available_chat_models(self) -> list[str]:
        return [mid for mid in self._chat_chain if mid in self._model_to_provider]

    def default_chat_model(self) -> str:
        chain = self.available_chat_models()
        return chain[0] if chain else ""

    def supports_chat(self, model_id: str) -> bool:
        """True if ``model_id`` is a registered chat model — the ModelRoutingService's rule-7 guard:
        an unknown/stale saved model id is filtered before a chain is walked."""
        return model_id in self._model_to_provider

    def supports_effort(self, model_id: str) -> bool:
        """True if ``model_id``'s provider honors a per-call ``effort`` (ADR-025 §4). Sourced from
        the provider, so the routing service + GET /settings never hardcode which models take
        effort."""
        provider = self._model_to_provider.get(model_id)
        return isinstance(provider, ChatProvider) and provider.supports_effort

    def _resolve_chain(self, requested: str | None, default_chain: list[str]) -> list[str]:
        """A requested model is tried first, then the remaining configured fallbacks."""
        if requested and requested in self._model_to_provider:
            rest = [mid for mid in default_chain if mid != requested]
            return [requested, *rest]
        return list(default_chain)

    async def _chat_over_chain(
        self,
        messages: list[ChatMessage],
        chain: list[str],
        effort_by_model: dict[str, str] | None = None,
    ) -> ChatResult:
        efforts = effort_by_model or {}
        errors: list[str] = []
        for index, model_id in enumerate(chain):
            provider = self._model_to_provider.get(model_id)
            if not isinstance(provider, ChatProvider):
                errors.append(f"{model_id}: not a chat model")
                continue
            try:
                # Per-model effort (ADR-025 §4): only models whose provider supports one get a
                # value; the rest receive None and use the provider default. The resolved model id
                # is passed so the one provider serves the right model (ADR-045).
                text = await provider.complete(
                    messages, model=model_id, effort=efforts.get(model_id)
                )
            except ProviderUnavailable as exc:
                logger.warning("chat model %s (%s) unavailable: %s", model_id, provider.id, exc)
                # Status is a PROVIDER signal (ADR-045 §6) — keyed by provider id, not model id.
                self._status.record_failure(provider.id, str(exc))
                errors.append(str(exc))
                continue
            self._status.record_success(provider.id)
            return ChatResult(
                text=text,
                model_used=model_id,
                fallback_used=index > 0,
                effort_used=efforts.get(model_id),
            )
        raise RegistryExhausted("all chat models unavailable: " + "; ".join(errors))

    async def run_chain(
        self,
        messages: list[ChatMessage],
        *,
        chain: list[str],
        effort_by_model: dict[str, str] | None = None,
        requested_model: str | None = None,
    ) -> ChatResult:
        """Mechanics for the ModelRoutingService (ADR-025 §3): walk an explicit ``chain`` of model
        ids (a requested model tried first), threading each model's ``effort``, recording
        ``model_used``/``fallback_used``. The routing *brain* (which chain, what effort) lives in
        the service; the registry stays pure provider mechanics."""
        resolved = self._resolve_chain(requested_model, chain)
        return await self._chat_over_chain(messages, resolved, effort_by_model)

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
        # Embedding has no fallback (single provider), so a failure here is a total outage that was
        # previously recorded nowhere — the most important ADR-044 blind spot to close. Record then
        # re-raise (the caller still sees the ProviderUnavailable; nothing is swallowed, rule 3).
        try:
            vectors = await provider.embed(texts)
        except ProviderUnavailable as exc:
            self._status.record_failure(self._embedding_provider_id, str(exc))
            raise
        self._status.record_success(self._embedding_provider_id)
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
                self._status.record_failure(provider_id, str(exc))
                errors.append(str(exc))
                continue
            self._status.record_success(provider_id)
            return TranscriptResult(text=text, model_used=provider_id, fallback_used=index > 0)
        raise RegistryExhausted("all STT providers unavailable: " + "; ".join(errors))

    async def provider_report(self) -> list[ProviderReport]:
        """One row per registered provider for ``GET /admin/providers`` (ADR-044).

        Probes every provider's ``health()`` **concurrently** (``asyncio.gather``) — finally using
        the dormant, LLM-free reachability seam — and folds in the in-memory status. ``reachable``
        is config-reachability, **not** a success guarantee (it would show a mis-configured provider
        green while every call fails); ``last_error``/``consecutive_failures`` carry the runtime
        truth beside it. Registration order is preserved.
        """
        items = list(self._providers.items())
        reachabilities = await asyncio.gather(*(self._probe_health(p) for _, p in items))
        reports: list[ProviderReport] = []
        for (pid, provider), reachable in zip(items, reachabilities, strict=True):
            status = self._status.status_for(pid)
            reports.append(
                ProviderReport(
                    id=pid,
                    label=getattr(provider, "provider_label", "") or pid,
                    capabilities=_provider_capabilities(provider),
                    reachable=reachable,
                    last_error=status.last_error,
                    last_success_at=status.last_success_at,
                    consecutive_failures=status.consecutive_failures,
                )
            )
        return reports

    @staticmethod
    async def _probe_health(provider: Provider) -> bool:
        """``health()`` is contractually non-raising and LLM-free; guard anyway so one misbehaving
        provider can't fail the whole report (a raise here means "not reachable")."""
        try:
            return await provider.health()
        except Exception:  # noqa: BLE001 — defensive coercion of a non-raising contract to a bool
            logger.warning("provider %s health probe raised (treating as unreachable)", provider.id)
            return False


def build_registry(settings: Settings) -> ProviderRegistry:
    """Construct the registry from settings — the only place providers are instantiated."""
    # OpenAI is STT fallback only now — embeddings moved to the self-hosted Ollama provider
    # (ADR-022), so no embedding_model here.
    openai = OpenAICompatibleProvider(
        id="openai",
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        stt_model=settings.stt_model,
        provider_label="OpenAI",
    )
    nebius = OpenAICompatibleProvider(
        id="nebius",
        base_url=settings.nebius_base_url,
        api_key=settings.nebius_api_key,
        default_chat_model=settings.nebius_chat_model,
        provider_label="Nebius",
    )
    # Groq — STT primary (ADR-020). Same OpenAI-compatible class, different endpoint/key/model.
    groq = OpenAICompatibleProvider(
        id="groq",
        base_url=settings.groq_base_url,
        api_key=settings.groq_api_key,
        stt_model=settings.groq_stt_model,
        provider_label="Groq",
    )
    # ONE `claude` provider serving BOTH Opus + Sonnet over the same CLI via per-call `--model`
    # (ADR-045 — collapses the former two fake single-model provider ids). The `quick` group routes
    # to the Sonnet model; `chat`/`conspect` to Opus. A per-call effort still overrides the
    # `claude_effort` seed.
    claude = ClaudeProvider(
        id="claude",
        models=[settings.claude_opus_model, settings.claude_sonnet_model],
        default_model=settings.claude_opus_model,
        effort=settings.claude_effort,
        provider_label="Claude",
    )
    # Self-hosted nomic embeddings (ADR-022): OpenAI-compatible /v1/embeddings on the on-box
    # Ollama sidecar, no API key (localhost). Single embedding provider — one index, one space.
    ollama = OpenAICompatibleProvider(
        id="ollama",
        base_url=settings.ollama_base_url,
        api_key="",
        embedding_model=settings.embedding_model,
        requires_api_key=False,
        provider_label="Ollama",
    )

    providers: dict[str, Provider] = {
        "openai": openai,
        "nebius": nebius,
        "groq": groq,
        "claude": claude,
        "ollama": ollama,
    }
    return ProviderRegistry(
        providers,
        chat_chain=settings.chat_chain,
        distill_chain=settings.distill_chain,
        embedding_provider_id=settings.embedding_provider_id,
        stt_chain=settings.stt_chain,
    )
