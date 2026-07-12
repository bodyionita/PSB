"""Fake providers for service tests — no live LLMs in CI (08 testing policy)."""

from __future__ import annotations

import asyncio
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
from app.services.git_repo import PushOutcome


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


class FakeGitRepo:
    """In-memory GitClient for VaultBackupService orchestration tests (no real git).

    Push behaviour is scriptable: ``non_ff_times`` rejects that many pushes as non-fast-forward
    (to exercise heal-on-reject), and ``push_ok`` controls the plain-failure path."""

    def __init__(
        self, *, is_repo: bool = True, has_head: bool = True, has_remote: bool = True
    ) -> None:
        self._is_repo = is_repo
        self._has_head = has_head
        self._has_remote = has_remote
        self._staged = False
        self.staged_after_add = True  # what add_all leaves staged (set False for "no changes")
        self.config: dict[str, str] = {}
        self.commits: list[str] = []
        self.pushes = 0
        self.pulls = 0
        self.aborts = 0
        self.inited = False
        self.non_ff_times = 0
        self.push_ok = True
        self.pull_ok = True
        self._merging = False
        # Optional gates to drive the concurrency regression test: commit() sets `commit_entered`
        # on entry and, if `commit_gate` is set, blocks until it is fired.
        self.commit_entered: asyncio.Event | None = None
        self.commit_gate: asyncio.Event | None = None

    async def is_repo(self) -> bool:
        return self._is_repo

    async def has_head(self) -> bool:
        return self._has_head

    async def init(self, branch: str) -> None:
        self.inited = True
        self._is_repo = True

    async def set_config(self, key: str, value: str) -> None:
        self.config[key] = value

    async def has_remote(self, name: str) -> bool:
        return self._has_remote

    async def add_all(self) -> None:
        self._staged = self.staged_after_add

    async def has_staged_changes(self) -> bool:
        return self._staged

    async def commit(self, message: str) -> None:
        if self.commit_entered is not None:
            self.commit_entered.set()
        if self.commit_gate is not None:
            await self.commit_gate.wait()
        self.commits.append(message)
        self._staged = False
        self._has_head = True

    async def push(self, remote: str, branch: str, *, set_upstream: bool = False) -> PushOutcome:
        self.pushes += 1
        if self.non_ff_times > 0:
            self.non_ff_times -= 1
            return PushOutcome(ok=False, non_fast_forward=True)
        if not self.push_ok:
            return PushOutcome(ok=False, non_fast_forward=False)
        return PushOutcome(ok=True)

    async def pull_merge(self, remote: str, branch: str) -> bool:
        self.pulls += 1
        return self.pull_ok

    async def is_merging(self) -> bool:
        return self._merging

    async def abort_merge(self) -> bool:
        self.aborts += 1
        self._merging = False
        return True

    async def head_sha(self) -> str | None:
        return "deadbeef" if self._has_head else None


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
