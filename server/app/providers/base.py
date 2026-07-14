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

    @abstractmethod
    async def health(self) -> bool:
        """Cheap availability check. Must never raise and must not perform a paid LLM call."""


class ChatProvider(Provider):
    # Whether this provider can actually serve chat. A provider may inherit ``ChatProvider`` for its
    # class hierarchy yet have no chat model configured (the OpenAI-compatible class is also the
    # STT/embedding provider — ADR-004): those are chat-capable by type but not by configuration.
    # The registry filters ``GET /chat/models`` on this so the picker never offers a non-chat model.
    can_chat: bool = True

    # Whether this provider honors a per-call reasoning ``effort`` (ADR-025 §4). Only models with
    # a native effort control (the Claude Max CLI's ``--effort``) set this True; the registry uses
    # it to route a group's effort only to providers that support one, and GET /settings to render
    # the effort control only where it applies. Providers without one ignore the arg.
    supports_effort: bool = False

    # The valid ``effort`` values for this provider, most→least or the provider's own order — the
    # source for the Settings effort selector (ADR-025 §6: no hardcoded effort enums in the web).
    # Empty when ``supports_effort`` is False.
    effort_levels: tuple[str, ...] = ()

    # Human-readable display name for the model picker (GET /chat/models, GET /settings — 03-api
    # §Chat/§Settings). Provider-sourced (like ``supports_effort``) so no router hardcodes a label;
    # concrete providers set it to their configured model in ``__init__``. Empty ⇒ fall back to id.
    label: str = ""

    @abstractmethod
    async def complete(
        self, messages: list[ChatMessage], *, model: str | None = None, effort: str | None = None
    ) -> str:
        """Return the assistant text, or raise ProviderUnavailable to trigger fallback.

        ``effort`` is the per-call reasoning effort (ADR-025 §4); providers that don't support one
        (``supports_effort`` False) ignore it and fall back to their construction default."""


class EmbeddingProvider(Provider):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, or raise ProviderUnavailable."""


class STTProvider(Provider):
    @abstractmethod
    async def transcribe(self, audio: bytes, *, filename: str) -> str:
        """Return the transcript text, or raise ProviderUnavailable."""
