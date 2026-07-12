"""Fake providers for service tests — no live LLMs in CI (08 testing policy)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.providers.base import (
    ChatMessage,
    ChatProvider,
    EmbeddingProvider,
    ProviderUnavailable,
    STTProvider,
)
from app.services.capture_store import FAILED, RECEIVED, TERMINAL_STATUSES, CaptureRecord


class FakeChatProvider(ChatProvider):
    """A chat provider that answers with a fixed reply, a per-message responder, or is down.

    ``responder`` (given the message list) wins over ``reply`` — it lets one provider return
    different bodies for different prompts (e.g. organizer JSON vs a nudge question)."""

    def __init__(
        self,
        id: str,
        *,
        reply: str | None = None,
        responder: Callable[[list[ChatMessage]], str] | None = None,
        available: bool = True,
    ) -> None:
        self.id = id
        self._reply = reply if reply is not None else f"answer from {id}"
        self._responder = responder
        self._available = available
        self.calls = 0

    async def health(self) -> bool:
        return self._available

    async def complete(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        self.calls += 1
        if not self._available:
            raise ProviderUnavailable(f"{self.id} is down")
        if self._responder is not None:
            return self._responder(messages)
        return self._reply


class FakeSTTProvider(STTProvider):
    """Speech-to-text fake: returns a fixed transcript, or is unavailable (STT-down path)."""

    def __init__(
        self, id: str = "fake-stt", *, transcript: str = "hello world", available: bool = True
    ) -> None:
        self.id = id
        self._transcript = transcript
        self._available = available
        self.calls = 0

    async def health(self) -> bool:
        return self._available

    async def transcribe(self, audio: bytes, *, filename: str) -> str:
        self.calls += 1
        if not self._available:
            raise ProviderUnavailable(f"{self.id} is down")
        return self._transcript


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, id: str = "fake-embed", dim: int = 8) -> None:
        self.id = id
        self._dim = dim

    async def health(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] * self._dim for t in texts]


@dataclass
class FakeVaultBackup:
    """Records commit requests instead of touching git (satisfies the VaultBackup protocol)."""

    reasons: list[str] = field(default_factory=list)

    async def request_commit(self, reason: str) -> None:
        self.reasons.append(reason)


class FakeCaptureStore:
    """In-memory CaptureStore for pipeline tests — no live DB (08 testing policy)."""

    def __init__(self) -> None:
        self.records: dict[str, CaptureRecord] = {}

    async def create(
        self,
        *,
        capture_id: str,
        kind: str,
        status: str,
        raw_text: str | None = None,
        audio_path: str | None = None,
        created_at: datetime | None = None,
    ) -> CaptureRecord:
        now = created_at or datetime.now(UTC)
        record = CaptureRecord(
            id=capture_id,
            kind=kind,
            status=status,
            raw_text=raw_text,
            audio_path=audio_path,
            created_at=now,
            updated_at=now,
        )
        self.records[capture_id] = record
        return record

    async def get(self, capture_id: str) -> CaptureRecord | None:
        return self.records.get(capture_id)

    async def list_recent(self, limit: int) -> list[CaptureRecord]:
        ordered = sorted(
            self.records.values(), key=lambda r: r.created_at or datetime.now(UTC), reverse=True
        )
        return ordered[:limit]

    async def mark_status(self, capture_id: str, status: str) -> None:
        self.records[capture_id].status = status

    async def mark_failed(self, capture_id: str, error: str) -> None:
        rec = self.records[capture_id]
        rec.status = FAILED
        rec.error = error

    async def set_raw_text(self, capture_id: str, raw_text: str) -> None:
        self.records[capture_id].raw_text = raw_text

    async def set_note_paths(self, capture_id: str, note_paths: list[str]) -> None:
        self.records[capture_id].note_paths = list(note_paths)

    async def set_follow_up_question(self, capture_id: str, question: str) -> None:
        self.records[capture_id].follow_up_question = question

    async def set_follow_up_answer(self, capture_id: str, answer: str) -> None:
        self.records[capture_id].follow_up_answer = answer

    async def reset_for_retry(self, capture_id: str) -> None:
        rec = self.records[capture_id]
        rec.status = RECEIVED
        rec.error = None

    async def sweep_orphans(self, error: str) -> int:
        count = 0
        for rec in self.records.values():
            if rec.status not in TERMINAL_STATUSES:
                rec.status = FAILED
                rec.error = error
                count += 1
        return count
