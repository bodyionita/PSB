"""Provider interfaces and shared data types (ADR-004).

Three capabilities, each optional per provider:
  * ChatProvider      — text completion / distillation
  * EmbeddingProvider — text embeddings (self-hosted nomic via Ollama, ADR-022)
  * STTProvider       — speech-to-text (Groq primary, OpenAI Whisper fallback)

A provider that lacks a capability simply doesn't implement that base class; the registry
routes each task only to providers that support it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderUnavailable(Exception):
    """Raised when a provider cannot serve a request (rate limit, timeout, no creds, error).

    The registry catches this to advance the fallback chain and record ``fallback_used``.
    """


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class ChatResult:
    text: str
    # Registry-level fields, filled in by ProviderRegistry, not the leaf provider:
    model_used: str = ""
    fallback_used: bool = False
    # The reasoning effort actually threaded to the winning provider (None for effort-less providers
    # like Nebius). Surfaced by chat for the "answered by <model> · <effort>" caption (ADR-025 §4).
    effort_used: str | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model_used: str = ""


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    # Registry-level fields, filled in by ProviderRegistry, not the leaf provider (ADR-020):
    model_used: str = ""
    fallback_used: bool = False


@dataclass
class Provider(ABC):
    """Common base: a stable id and a cheap, non-raising health probe."""

    id: str = field(default="")

    # Friendly PROVIDER display name for the ADR-044 Providers card (GET /admin/providers) — one
    # row per provider, labeled here ("Claude"/"Nebius"/…), NOT per model (ADR-045 §6). This is
    # distinct from a model label (which the registry derives per model id via labels.py); a
    # provider now serves N models so it can't carry a single model label. Empty ⇒ fall back to id.
    provider_label: str = field(default="")

    @abstractmethod
    async def health(self) -> bool:
        """Cheap availability check. Must never raise and must not perform a paid LLM call."""


class ChatProvider(Provider):
    # Whether this provider can actually serve chat. A provider may inherit ``ChatProvider`` for its
    # class hierarchy yet have no chat model configured (the OpenAI-compatible class is also the
    # STT/embedding provider — ADR-004): those are chat-capable by type but not by configuration.
    # The registry filters ``GET /chat/models`` on this so the picker never offers a non-chat model.
    can_chat: bool = True

    # Whether this provider honors a per-call reasoning ``effort`` (ADR-025 §4). Only providers with
    # a native effort control (the Claude CLI's ``--effort``) set this True; the registry uses it to
    # route a group's effort only to models whose provider supports one, and GET /settings to render
    # the effort control only where it applies. Providers without one ignore the arg.
    supports_effort: bool = False

    # The valid ``effort`` values for this provider, most→least or the provider's own order — the
    # source for the Settings effort selector (ADR-025 §6: no hardcoded effort enums in the web).
    # Empty when ``supports_effort`` is False.
    effort_levels: tuple[str, ...] = ()

    def chat_model_ids(self) -> tuple[str, ...]:
        """The chat model ids this provider serves (empty when not chat-configured). One provider
        may serve N models via per-call ``model=`` (ADR-045 — e.g. ``claude`` serves Opus+Sonnet);
        the registry builds its chat-model catalog + model→provider index from this. The default
        serves a single model whose id is the provider id (fits Nebius-style one-model providers and
        test fakes); concrete multi-model providers override."""
        return (self.id,) if self.can_chat else ()

    @abstractmethod
    async def complete(
        self, messages: list[ChatMessage], *, model: str | None = None, effort: str | None = None
    ) -> str:
        """Return the assistant text, or raise ProviderUnavailable to trigger fallback.

        ``model`` is the vendor model string to serve this call (ADR-045 — the registry passes the
        resolved model id, so one provider can serve N models); ``None`` ⇒ the provider's default.
        ``effort`` is the per-call reasoning effort (ADR-025 §4); providers that don't support one
        (``supports_effort`` False) ignore it and fall back to their construction default."""


class EmbeddingProvider(Provider):
    # Whether this provider is actually configured to embed. Mirrors ``ChatProvider.can_chat``:
    # the OpenAI-compatible class backs every capability by type, but only an instance with a
    # configured embedding model can embed. Feeds the ADR-044 ``capabilities`` list so the
    # provider-observability row reflects configuration, not merely the class hierarchy.
    can_embed: bool = True

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, or raise ProviderUnavailable."""


class STTProvider(Provider):
    # Whether this provider is actually configured to transcribe (mirrors ``can_chat``/``can_embed``
    # — see EmbeddingProvider). Feeds the ADR-044 ``capabilities`` list.
    can_transcribe: bool = True

    @abstractmethod
    async def transcribe(self, audio: bytes, *, filename: str) -> str:
        """Return the transcript text, or raise ProviderUnavailable."""
