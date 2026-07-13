"""Fake providers for service tests — no live LLMs in CI (08 testing policy)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.graph.store import NoteNeighbors
from app.indexing.indexer import IndexOutcome
from app.indexing.store import NoteUpsert
from app.providers.base import (
    ChatMessage,
    ChatProvider,
    EmbeddingProvider,
    ProviderUnavailable,
    STTProvider,
)
from app.search.store import NoteRow, SearchHit
from app.services.agent_runs import RUNNING, AgentRun
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
    """Deterministic embedder: each text → ``[len(text)] * dim``. Records the exact inputs (so a
    test can assert the ``search_document:`` prefix) and can be flipped unavailable."""

    def __init__(self, id: str = "fake-embed", dim: int = 8, *, available: bool = True) -> None:
        self.id = id
        self._dim = dim
        self._available = available
        self.inputs: list[list[str]] = []

    async def health(self) -> bool:
        return self._available

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.inputs.append(list(texts))
        if not self._available:
            raise ProviderUnavailable(f"{self.id} is down")
        return [[float(len(t))] * self._dim for t in texts]


class FakeIndexStore:
    """In-memory IndexStore for indexer tests — no live DB (08 testing policy). Keeps the last
    upserted :class:`NoteUpsert` per path so a test can inspect chunks / mean-pooled embedding."""

    def __init__(self) -> None:
        self.notes: dict[str, NoteUpsert] = {}

    async def get_content_hash(self, vault_path: str) -> str | None:
        note = self.notes.get(vault_path)
        return note.content_hash if note is not None else None

    async def upsert_note(self, note: NoteUpsert) -> None:
        self.notes[note.vault_path] = note

    async def list_indexed_paths(self) -> set[str]:
        return set(self.notes)

    async def delete_notes(self, vault_paths: list[str]) -> int:
        count = 0
        for path in vault_paths:
            if self.notes.pop(path, None) is not None:
                count += 1
        return count


class FakeSearchStore:
    """In-memory SearchStore for search-service tests — no live DB. Returns preset hits/note and
    records the exact search arguments so a test can assert clamping / plane-filter handling."""

    def __init__(
        self, *, hits: list[SearchHit] | None = None, note: NoteRow | None = None
    ) -> None:
        self._hits = hits or []
        self._note = note
        self.search_args: dict | None = None

    async def search_chunks(
        self,
        embedding: list[float],
        *,
        top_k: int,
        planes: list[str] | None,
        min_score: float,
    ) -> list[SearchHit]:
        self.search_args = {
            "embedding": embedding,
            "top_k": top_k,
            "planes": planes,
            "min_score": min_score,
        }
        return list(self._hits)

    async def get_note(self, note_id: str) -> NoteRow | None:
        if self._note is not None and self._note.note_id == note_id:
            return self._note
        return None


class FakeIndexer:
    """Records the paths the capture pipeline asks it to index; returns a clean IndexOutcome."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def index_paths(self, vault_paths: list[str]) -> IndexOutcome:
        self.calls.append(list(vault_paths))
        return IndexOutcome(indexed=len(vault_paths))


@dataclass
class FakeVaultBackup:
    """Records commit requests instead of touching git (satisfies the VaultBackup protocol)."""

    reasons: list[str] = field(default_factory=list)

    async def request_commit(self, reason: str) -> None:
        self.reasons.append(reason)


class FakeGraphStore:
    """In-memory GraphStore for relatedness-graph tests — no live DB (08 testing policy).

    ``neighbors`` is the preset output of ``compute_neighbors`` (records the args it was called
    with); ``paths`` is the note universe for the render pass; ``written_links`` captures the
    edges the service asked to materialize."""

    def __init__(
        self,
        *,
        neighbors: list[NoteNeighbors] | None = None,
        paths: list[str] | None = None,
    ) -> None:
        self._neighbors = neighbors or []
        self._paths = paths if paths is not None else [n.vault_path for n in self._neighbors]
        self.compute_args: dict | None = None
        self.written_links: list[NoteNeighbors] | None = None

    async def compute_neighbors(self, *, top_k: int, min_score: float) -> list[NoteNeighbors]:
        self.compute_args = {"top_k": top_k, "min_score": min_score}
        return list(self._neighbors)

    async def replace_note_links(self, neighbors: list[NoteNeighbors]) -> int:
        self.written_links = list(neighbors)
        return sum(len(n.related) for n in neighbors)

    async def list_note_paths(self) -> list[str]:
        return list(self._paths)


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
        self.commit_count_value = 3
        self.file_count_value = 5
        self.bundles: list[str] = []
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

    async def commit_count(self) -> int:
        return self.commit_count_value

    async def tracked_file_count(self) -> int:
        return self.file_count_value

    async def bundle_all(self, path: str) -> None:
        # Write a placeholder so the job's read_bytes()/upload path exercises real file IO.
        from pathlib import Path

        Path(path).write_bytes(b"FAKE-BUNDLE")  # noqa: ASYNC240 — trivial test fake, not prod IO
        self.bundles.append(path)


class FakeObjectStore:
    """In-memory ObjectStore for backup-job tests — no boto3, no network."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]  # KeyError signals a missing object (job fails → visible)

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


class FakeAgentRunStore:
    """In-memory AgentRunStore for backup-job tests. `preloaded` overrides latest(agent)."""

    def __init__(self) -> None:
        self.runs: dict[str, AgentRun] = {}
        self.preloaded: dict[str, AgentRun] = {}
        self._seq = 0

    async def start(self, agent: str) -> str:
        self._seq += 1
        run_id = f"run-{self._seq}"
        self.runs[run_id] = AgentRun(id=run_id, agent=agent, status=RUNNING)
        return run_id

    async def finish(
        self,
        run_id,
        *,
        status,
        summary=None,
        details=None,
        error=None,
        model_used=None,
        fallback_used=False,
    ) -> None:
        run = self.runs[run_id]
        run.status = status
        run.summary = summary
        run.details = details or {}
        run.error = error
        run.model_used = model_used
        run.fallback_used = fallback_used

    async def latest(self, agent: str, *, status: str | None = None) -> AgentRun | None:
        if agent in self.preloaded:
            run = self.preloaded[agent]
            return run if status is None or run.status == status else None
        matching = [
            r for r in self.runs.values()
            if r.agent == agent and (status is None or r.status == status)
        ]
        return matching[-1] if matching else None


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
