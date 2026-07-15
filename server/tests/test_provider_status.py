"""Provider-observability status tracking + GET /admin/providers wiring (ADR-044).

Covers the `ProviderStatusTracker` collaborator in isolation (sticky last-error, success reset,
truncation, injected clock), the `record_*` wiring at all three registry call sites
(chat/STT/embedding) including the forced-failure→recovery path the M4 Accept exposed, and the
`provider_report()` shape (capabilities from configuration, live health probe, folded status).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.providers.base import (
    ChatMessage,
    ChatProvider,
    EmbeddingProvider,
    ProviderUnavailable,
    STTProvider,
)
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import ProviderRegistry, build_registry
from app.providers.status import MAX_ERROR_MESSAGE_LEN, ProviderStatusTracker


class _Clock:
    """A controllable monotonic clock — each call advances one second from a fixed epoch."""

    def __init__(self) -> None:
        self.t = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


# --- ProviderStatusTracker in isolation --------------------------------------------------------
def test_unknown_provider_is_clean_zero_state():
    tracker = ProviderStatusTracker()
    status = tracker.status_for("never-seen")
    assert status.last_error is None
    assert status.last_success_at is None
    assert status.consecutive_failures == 0


def test_failure_sets_sticky_error_and_bumps_counter():
    clock = _Clock()
    tracker = ProviderStatusTracker(now=clock)
    tracker.record_failure("nebius", "nebius chat failed: 404")
    tracker.record_failure("nebius", "nebius chat failed: 500")
    status = tracker.status_for("nebius")
    assert status.consecutive_failures == 2
    assert status.last_error is not None
    # Latest message wins; timestamp is the injected clock's.
    assert status.last_error.message == "nebius chat failed: 500"
    assert status.last_error.at == datetime(2026, 7, 15, 12, 0, 2, tzinfo=UTC)
    assert status.last_success_at is None


def test_success_resets_counter_but_last_error_stays_sticky():
    clock = _Clock()
    tracker = ProviderStatusTracker(now=clock)
    tracker.record_failure("nebius", "boom")
    tracker.record_success("nebius")
    status = tracker.status_for("nebius")
    # The forensic trail is preserved (ADR-044 decision 4): last_error is NOT cleared by a success.
    assert status.last_error is not None
    assert status.last_error.message == "boom"
    # But the "broken right now?" signal is clean.
    assert status.consecutive_failures == 0
    assert status.last_success_at == datetime(2026, 7, 15, 12, 0, 2, tzinfo=UTC)


def test_error_message_is_truncated():
    tracker = ProviderStatusTracker()
    tracker.record_failure("p", "x" * 5000)
    status = tracker.status_for("p")
    assert status.last_error is not None
    assert len(status.last_error.message) == MAX_ERROR_MESSAGE_LEN


# --- registry call-site wiring -----------------------------------------------------------------
class _FakeChat(ChatProvider):
    def __init__(self, id: str, *, healthy: bool = True, provider_label: str = "") -> None:
        self.id = id
        self.provider_label = provider_label
        self.can_chat = True
        self.fail = False
        self._healthy = healthy

    async def health(self) -> bool:
        return self._healthy

    async def complete(self, messages, *, model=None, effort=None) -> str:
        if self.fail:
            raise ProviderUnavailable(f"{self.id}: boom")
        return "ok"


class _FakeEmbed(EmbeddingProvider):
    def __init__(self, id: str) -> None:
        self.id = id
        self.can_embed = True
        self.fail = False

    async def health(self) -> bool:
        return True

    async def embed(self, texts):
        if self.fail:
            raise ProviderUnavailable(f"{self.id}: down")
        return [[0.0] for _ in texts]


class _FakeSTT(STTProvider):
    def __init__(self, id: str) -> None:
        self.id = id
        self.can_transcribe = True
        self.fail = False

    async def health(self) -> bool:
        return True

    async def transcribe(self, audio, *, filename) -> str:
        if self.fail:
            raise ProviderUnavailable(f"{self.id}: stt down")
        return "text"


def _registry(providers, *, chat_chain=None, stt_chain=None, embed_id="emb", tracker=None):
    return ProviderRegistry(
        {p.id: p for p in providers},
        chat_chain=chat_chain or [],
        distill_chain=[],
        embedding_provider_id=embed_id,
        stt_chain=stt_chain or [],
        status_tracker=tracker,
    )


async def test_chat_records_failure_of_first_and_success_of_fallback():
    primary, fallback = _FakeChat("primary"), _FakeChat("fallback")
    primary.fail = True
    tracker = ProviderStatusTracker()
    reg = _registry([primary, fallback], chat_chain=["primary", "fallback"], tracker=tracker)

    result = await reg.chat([ChatMessage(role="user", content="hi")])
    assert result.model_used == "fallback" and result.fallback_used is True

    # The silent fallback is now visible: the primary's error is captured, the winner's success is.
    assert tracker.status_for("primary").consecutive_failures == 1
    assert tracker.status_for("primary").last_error is not None
    assert tracker.status_for("fallback").last_success_at is not None
    assert tracker.status_for("fallback").consecutive_failures == 0


async def test_chat_forced_failure_then_recovery(monkeypatch):
    """The M4-Accept reproduction: a provider fails (bad model id), then recovers. last_error is
    sticky across the recovery; consecutive_failures + last_success_at track the flip."""
    clock = _Clock()
    provider = _FakeChat("nebius")
    tracker = ProviderStatusTracker(now=clock)
    reg = _registry([provider], chat_chain=["nebius"], tracker=tracker)

    provider.fail = True
    with pytest.raises(ProviderUnavailable):
        await reg.chat([ChatMessage(role="user", content="hi")])
    assert tracker.status_for("nebius").consecutive_failures == 1
    assert tracker.status_for("nebius").last_success_at is None

    provider.fail = False
    await reg.chat([ChatMessage(role="user", content="hi")])
    status = tracker.status_for("nebius")
    assert status.consecutive_failures == 0
    assert status.last_success_at is not None
    assert status.last_error is not None  # sticky — the outage record survives recovery


async def test_embed_failure_recorded_and_reraised():
    emb = _FakeEmbed("emb")
    emb.fail = True
    tracker = ProviderStatusTracker()
    reg = _registry([emb], embed_id="emb", tracker=tracker)

    # Embedding has no fallback — a failure is a total outage, previously recorded nowhere.
    with pytest.raises(ProviderUnavailable):
        await reg.embed(["hello"])
    assert tracker.status_for("emb").consecutive_failures == 1
    assert tracker.status_for("emb").last_error is not None


async def test_embed_success_recorded():
    emb = _FakeEmbed("emb")
    tracker = ProviderStatusTracker()
    reg = _registry([emb], embed_id="emb", tracker=tracker)
    await reg.embed(["hello"])
    assert tracker.status_for("emb").last_success_at is not None
    assert tracker.status_for("emb").consecutive_failures == 0


async def test_stt_records_success_and_failure():
    primary, fallback = _FakeSTT("groq"), _FakeSTT("openai")
    primary.fail = True
    tracker = ProviderStatusTracker()
    reg = _registry([primary, fallback], stt_chain=["groq", "openai"], tracker=tracker)
    result = await reg.transcribe(b"audio", filename="a.wav")
    assert result.model_used == "openai" and result.fallback_used is True
    assert tracker.status_for("groq").consecutive_failures == 1
    assert tracker.status_for("openai").last_success_at is not None


# --- provider_report ---------------------------------------------------------------------------
async def test_provider_report_shape_and_status_fold():
    healthy = _FakeChat("primary", provider_label="Primary Model")
    unhealthy = _FakeChat("fallback", healthy=False)
    tracker = ProviderStatusTracker()
    reg = _registry([healthy, unhealthy], chat_chain=["primary", "fallback"], tracker=tracker)
    tracker.record_failure("fallback", "fallback: boom")

    report = await reg.provider_report()
    by_id = {r.id: r for r in report}

    assert by_id["primary"].label == "Primary Model"  # sourced from provider_label (ADR-045 §6)
    assert by_id["primary"].capabilities == ["chat"]
    assert by_id["primary"].reachable is True
    # Live health() probe is reachability, not success: the unhealthy provider reads not-reachable,
    # and its captured runtime error is folded in beside it.
    assert by_id["fallback"].reachable is False
    assert by_id["fallback"].last_error is not None
    assert by_id["fallback"].consecutive_failures == 1


async def test_provider_report_health_probe_that_raises_is_unreachable():
    class _Raises(_FakeChat):
        async def health(self) -> bool:
            raise RuntimeError("health blew up")

    reg = _registry([_Raises("x")], chat_chain=["x"])
    report = await reg.provider_report()
    assert report[0].reachable is False  # defensive: a raising probe → not reachable, never a 500


async def test_provider_report_label_falls_back_to_id():
    reg = _registry([_FakeEmbed("emb")], embed_id="emb")
    report = await reg.provider_report()
    assert report[0].label == "emb"  # no .label attr on the fake → id


def test_capabilities_reflect_configuration_not_class_hierarchy():
    """The OpenAI-compatible class backs all three capabilities by type; only the configured ones
    should appear (openai=stt, nebius=chat, ollama=embedding)."""
    stt = OpenAICompatibleProvider(id="openai", base_url="https://x/v1", api_key="k", stt_model="w")
    chat = OpenAICompatibleProvider(
        id="nebius", base_url="https://x/v1", api_key="k", default_chat_model="m"
    )
    embed = OpenAICompatibleProvider(
        id="ollama", base_url="http://x/v1", api_key="", embedding_model="e", requires_api_key=False
    )
    assert (stt.can_transcribe, stt.can_chat, stt.can_embed) == (True, False, False)
    assert (chat.can_chat, chat.can_transcribe, chat.can_embed) == (True, False, False)
    assert (embed.can_embed, embed.can_chat, embed.can_transcribe) == (True, False, False)


async def test_build_registry_reports_every_configured_provider():
    reg = build_registry(Settings())
    report = await reg.provider_report()
    caps = {r.id: r.capabilities for r in report}
    labels = {r.id: r.label for r in report}
    # FIVE providers now — `claude` collapsed to one row (ADR-045 §6), no fake `-max`/`-sonnet`.
    assert set(caps) == {"openai", "nebius", "groq", "claude", "ollama"}
    assert caps["nebius"] == ["chat"]
    assert caps["claude"] == ["chat"]  # one row serving both Opus + Sonnet
    assert caps["openai"] == ["stt"]
    assert caps["groq"] == ["stt"]
    assert caps["ollama"] == ["embedding"]
    # Provider-name labels, not raw ids or model names (ADR-045 §6).
    assert labels["claude"] == "Claude"
    assert labels["nebius"] == "Nebius"
