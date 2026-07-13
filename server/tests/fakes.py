"""Fakes for service tests — no live LLMs/DB in CI (08 testing policy)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.entities.store import EntityCandidate, normalize_alias
from app.graph.store import SimilarEdge
from app.indexing.indexer import IndexOutcome
from app.indexing.store import CanonicalEdge, IndexState, NodeUpsert
from app.providers.base import (
    ChatMessage,
    ChatProvider,
    EmbeddingProvider,
    ProviderUnavailable,
    STTProvider,
)
from app.search.store import NodeRow, SearchHit
from app.services.agent_runs import RUNNING, AgentRun
from app.services.capture_store import FAILED, RECEIVED, TERMINAL_STATUSES, CaptureRecord
from app.services.git_repo import PushOutcome
from app.services.review_queue import ReviewItem


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
    upserted :class:`NodeUpsert` per id, plus the materialized canonical edges per node."""

    def __init__(self) -> None:
        self.nodes: dict[str, NodeUpsert] = {}
        self.edges: dict[str, list[CanonicalEdge]] = {}
        self.path_updates: list[tuple[str, str]] = []

    async def get_index_state(self, node_id: str) -> IndexState | None:
        node = self.nodes.get(node_id)
        if node is None:
            return None
        return IndexState(content_hash=node.content_hash, store_path=node.store_path)

    async def upsert_node(self, node: NodeUpsert) -> None:
        self.nodes[node.id] = node

    async def update_node_path(self, node_id: str, store_path: str) -> None:
        self.path_updates.append((node_id, store_path))
        node = self.nodes.get(node_id)
        if node is not None:
            self.nodes[node_id] = replace_store_path(node, store_path)

    async def replace_canonical_edges(self, node_id: str, edges: list[CanonicalEdge]) -> int:
        # Mirror the real store: only materialize edges whose target node exists.
        kept = [e for e in edges if e.dst_id in self.nodes]
        self.edges[node_id] = kept
        return len(kept)

    async def list_indexed_paths(self) -> set[str]:
        return {n.store_path for n in self.nodes.values()}

    async def delete_nodes(self, store_paths: list[str]) -> int:
        targets = set(store_paths)
        gone = [nid for nid, n in self.nodes.items() if n.store_path in targets]
        for nid in gone:
            self.nodes.pop(nid, None)
            self.edges.pop(nid, None)
        return len(gone)


def replace_store_path(node: NodeUpsert, store_path: str) -> NodeUpsert:
    from dataclasses import replace

    return replace(node, store_path=store_path)


class FakeSearchStore:
    """In-memory SearchStore for search-service tests — no live DB. Returns preset hits/node and
    records the exact search arguments so a test can assert clamping / filter handling."""

    def __init__(
        self, *, hits: list[SearchHit] | None = None, node: NodeRow | None = None
    ) -> None:
        self._hits = hits or []
        self._node = node
        self.search_args: dict | None = None

    async def search_chunks(
        self,
        embedding: list[float],
        *,
        top_k: int,
        planes: list[str] | None,
        types: list[str] | None,
        min_score: float,
    ) -> list[SearchHit]:
        self.search_args = {
            "embedding": embedding,
            "top_k": top_k,
            "planes": planes,
            "types": types,
            "min_score": min_score,
        }
        return list(self._hits)

    async def get_node(self, node_id: str) -> NodeRow | None:
        if self._node is not None and self._node.node_id == node_id:
            return self._node
        return None


class FakeIndexer:
    """Records the paths the capture pipeline asks it to index; returns a clean IndexOutcome."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def index_paths(self, store_paths: list[str]) -> IndexOutcome:
        self.calls.append(list(store_paths))
        return IndexOutcome(indexed=len(store_paths))


@dataclass
class FakeStoreBackup:
    """Records commit requests instead of touching git (satisfies the StoreBackup protocol)."""

    reasons: list[str] = field(default_factory=list)

    async def request_commit(self, reason: str) -> None:
        self.reasons.append(reason)


class FakeGraphStore:
    """In-memory GraphStore for derived-edge tests — no live DB (08 testing policy).

    ``edges`` is the preset output of ``compute_similar`` (records the args); ``written`` captures
    the edges the service asked to materialize."""

    def __init__(self, *, edges: list[SimilarEdge] | None = None) -> None:
        self._edges = edges or []
        self.compute_args: dict | None = None
        self.written: list[SimilarEdge] | None = None

    async def compute_similar(self, *, top_k: int, min_score: float) -> list[SimilarEdge]:
        self.compute_args = {"top_k": top_k, "min_score": min_score}
        return list(self._edges)

    async def replace_derived_edges(self, edges: list[SimilarEdge]) -> int:
        self.written = list(edges)
        return len(edges)


class FakeTagStore:
    """In-memory TagStore for tag-vocabulary + consolidation tests — no live DB.

    ``counts`` seeds the vocabulary (tag → node frequency); ``nodes_by_tag`` maps a tag to the
    store paths carrying it (for the consolidation apply lookup)."""

    def __init__(
        self,
        *,
        counts: list[tuple[str, int]] | None = None,
        nodes_by_tag: dict[str, list[str]] | None = None,
    ) -> None:
        self._counts = counts or []
        self._nodes_by_tag = nodes_by_tag or {}
        self.vocab_calls: list[int] = []

    async def tag_counts(self, *, limit: int):
        from app.tags.store import TagCount

        self.vocab_calls.append(limit)
        return [TagCount(tag=t, count=n) for t, n in self._counts[:limit]]

    async def vocabulary_tags(self, *, limit: int) -> list[str]:
        return [tc.tag for tc in await self.tag_counts(limit=limit)]

    async def nodes_with_any_tag(self, tags: list[str]):
        from app.tags.store import TaggedNode

        paths: list[str] = []
        for tag in tags:
            for path in self._nodes_by_tag.get(tag, []):
                if path not in paths:
                    paths.append(path)
        return [TaggedNode(store_path=p) for p in sorted(paths)]


class FakeCommitBackup:
    """Records forced commit+push calls (the StoreCommitter surface the tags apply needs)."""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def backup_now(self, reason: str = "manual backup"):
        from app.services.store_backup import BackupResult

        self.reasons.append(reason)
        return BackupResult(committed=True, pushed=True)


class FakeAliasStore:
    """In-memory alias index for resolver tests. ``candidates_by_key`` maps a
    (normalized_name, type) to the candidates a mention resolves against."""

    def __init__(
        self, *, candidates_by_key: dict[tuple[str, str], list[EntityCandidate]] | None = None
    ) -> None:
        self._by_key = candidates_by_key or {}
        self.queries: list[tuple[str, tuple[str, ...]]] = []

    async def find_candidates(self, name: str, *, types: list[str]) -> list[EntityCandidate]:
        self.queries.append((name, tuple(types)))
        out: list[EntityCandidate] = []
        for t in types:
            out.extend(self._by_key.get((normalize_alias(name), t), []))
        return out


class FakeReviewQueue:
    """Records filed review items (the ReviewQueue write surface)."""

    def __init__(self) -> None:
        self.items: list[ReviewItem] = []
        self._seq = 0

    async def enqueue(self, item: ReviewItem) -> str:
        self._seq += 1
        self.items.append(item)
        return f"review-{self._seq}"


class FakeGitRepo:
    """In-memory GitClient for StoreBackupService orchestration tests (no real git).

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
        self.remotes: dict[str, str] = {}
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

    async def set_remote(self, name: str, url: str) -> None:
        self.remotes[name] = url
        self._has_remote = True

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
            r
            for r in self.runs.values()
            if r.agent == agent and (status is None or r.status == status)
        ]
        return matching[-1] if matching else None

    async def get(self, run_id: str) -> AgentRun | None:
        return self.runs.get(run_id)


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

    async def set_node_paths(self, capture_id: str, node_paths: list[str]) -> None:
        self.records[capture_id].node_paths = list(node_paths)

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
