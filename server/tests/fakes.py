"""Fakes for service tests — no live LLMs/DB in CI (08 testing policy)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.config import Settings
from app.entities.store import EntityCandidate, normalize_alias
from app.graph.store import NeighborCursor, NeighborEdge, SimilarEdge
from app.identity.store import CapsuleBlob, HubProfile, RecentNode
from app.indexing.indexer import IndexOutcome
from app.indexing.store import CanonicalEdge, IndexState, NodeUpsert
from app.providers.base import (
    ChatMessage,
    ChatProvider,
    EmbeddingProvider,
    ProviderUnavailable,
    STTProvider,
)
from app.providers.registry import ProviderRegistry
from app.search.service import NodePreview
from app.search.store import NodeRow, RetrievalParams, SearchHit
from app.services.agent_runs import (
    RUNNING,
    AgentRun,
    current_parent_run_id,
    record_child_run,
)
from app.services.capture_store import FAILED, RECEIVED, TERMINAL_STATUSES, CaptureRecord
from app.services.git_repo import PushOutcome
from app.services.model_routing import GroupRouting, ModelRoutingService
from app.services.review_queue import ReviewItem, ReviewRecord
from app.vocab.store import VocabularyAdditions


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
        supports_effort: bool = False,
        effort_levels: tuple[str, ...] = (),
        provider_label: str = "",
        can_chat: bool = True,
        models: list[str] | None = None,
    ) -> None:
        self.id = id
        # Friendly PROVIDER label for the ADR-044 card (ADR-045 §6). Model display labels are
        # derived per model id by the registry (labels.py), not carried on the provider.
        self.provider_label = provider_label
        self.can_chat = can_chat
        # The chat model ids this fake serves (ADR-045). Default = a single model whose id is the
        # provider id (fits most fallback/routing tests); pass ``models`` for the N-models-per-
        # provider case or to exercise real vendor-string ids / friendly labels.
        self._models = list(models) if models else None
        self._reply = reply if reply is not None else f"answer from {id}"
        self._responder = responder
        self._available = available
        self.supports_effort = supports_effort
        # Default the effort scale to the Claude scale when effort-capable and none given, so a
        # routing/settings test sees realistic levels without wiring them every time.
        self.effort_levels = effort_levels or (
            ("low", "medium", "high", "xhigh", "max") if supports_effort else ()
        )
        self.calls = 0
        # The per-call efforts this provider was asked for, in order (None when unset) — lets a
        # routing test assert the group's effort reached the right model (ADR-025 §4).
        self.efforts: list[str | None] = []
        # The per-call ``model=`` this provider was asked to serve, in order — lets a test assert
        # the registry passed the resolved model id (ADR-045, one provider serving N models).
        self.models_seen: list[str | None] = []
        # The messages of the most recent call (lets a test assert prompt/fencing content).
        self.last_messages: list[ChatMessage] = []

    def chat_model_ids(self) -> tuple[str, ...]:
        if not self.can_chat:
            return ()
        return tuple(self._models) if self._models else (self.id,)

    async def health(self) -> bool:
        return self._available

    async def complete(
        self, messages: list[ChatMessage], *, model: str | None = None, effort: str | None = None
    ) -> str:
        self.calls += 1
        self.efforts.append(effort)
        self.models_seen.append(model)
        self.last_messages = list(messages)
        if not self._available:
            raise ProviderUnavailable(f"{self.id} is down")
        if self._responder is not None:
            return self._responder(messages)
        return self._reply


class FakeModelRoutingStore:
    """In-memory ModelRoutingStore — the saved-override half of the routing brain (ADR-025 §3).

    Empty by default → the ModelRoutingService resolves from config seeds only; pass ``saved`` to
    simulate a user having pinned a group."""

    def __init__(self, saved: dict[str, GroupRouting] | None = None) -> None:
        self.saved: dict[str, GroupRouting] = dict(saved or {})

    async def get_all(self) -> dict[str, GroupRouting]:
        return dict(self.saved)

    async def save(self, group: str, routing: GroupRouting) -> None:
        self.saved[group] = routing


def fake_routing(
    registry: ProviderRegistry,
    *,
    chain: tuple[str, ...] = ("fake-chat",),
    store: FakeModelRoutingStore | None = None,
) -> ModelRoutingService:
    """A ModelRoutingService whose three groups all seed to ``chain`` (the test's fake ids).

    Service tests build a registry over fake providers; this wraps it so ``routing.complete`` lands
    on those same providers, standing in for the production ``build_model_routing`` wiring."""
    settings = Settings(chat_chain=list(chain), distill_chain=list(chain), quick_chain=list(chain))
    return ModelRoutingService(
        settings=settings, store=store or FakeModelRoutingStore(), registry=registry
    )


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
        self, embedding: list[float], query_text: str, params: RetrievalParams
    ) -> list[SearchHit]:
        # Flattened so tests can assert clamping / filter / query-text handling without reaching
        # into the params object; `params` kept too for any structural assertion.
        self.search_args = {
            "embedding": embedding,
            "query_text": query_text,
            "top_k": params.top_k,
            "candidates": params.candidates,
            "planes": params.planes,
            "types": params.types,
            "since": params.since,
            "until": params.until,
            "as_of": params.as_of,
            "min_score": params.min_score,
            "params": params,
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


class FakeNeighborStore:
    """In-memory NeighborStore for GraphService tests — no live DB (08 testing policy).

    ``edges`` maps a center node id to its 1-hop neighbors. It replicates the real store's
    ``(origin, rel, dir, node_id)`` ordering + keyset paging + ``rel``/``direction`` filters so the
    service's pagination/fanout logic is exercised end-to-end; ``calls`` records each invocation's
    args for assertions."""

    def __init__(self, *, edges: dict[str, list[NeighborEdge]] | None = None) -> None:
        self._edges = edges or {}
        self.calls: list[dict] = []

    async def neighbors(
        self,
        node_id: str,
        *,
        rel: str | None,
        direction: str | None,
        after: NeighborCursor | None,
        limit: int,
    ) -> list[NeighborEdge]:
        self.calls.append(
            {
                "node_id": node_id,
                "rel": rel,
                "direction": direction,
                "after": after,
                "limit": limit,
            }
        )
        rows = sorted(
            self._edges.get(node_id, []),
            key=lambda e: (e.origin, e.rel, e.dir, e.node_id),
        )
        if rel is not None:
            rows = [e for e in rows if e.rel == rel]
        if direction is not None:
            rows = [e for e in rows if e.dir == direction]
        if after is not None:
            rows = [e for e in rows if (e.origin, e.rel, e.dir, e.node_id) > after]
        return rows[:limit]


class FakeNodeReader:
    """In-memory NodeReader for build_context tests — a preset NodePreview by id, else None."""

    def __init__(self, *, nodes: dict[str, NodePreview] | None = None) -> None:
        self._nodes = nodes or {}

    async def get_node(self, node_id: str) -> NodePreview | None:
        return self._nodes.get(node_id)


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


class FakeEntityStore:
    """In-memory EntityStore for merge/backfill/profile tests — no live DB (08 testing policy).

    Seeded with entity ``EntityNode``s (by id), inbound canonical edges (by dst id), 1-hop
    neighborhoods (by node id), and alias-match candidates (by lower-cased alias)."""

    def __init__(
        self,
        *,
        nodes=None,
        inbound=None,
        entities=None,
        neighborhoods=None,
        alias_matches=None,
    ) -> None:
        self.nodes = dict(nodes or {})  # id -> EntityNode
        self._inbound = dict(inbound or {})  # dst_id -> list[InboundEdge]
        self._entities = list(entities or [])  # list[EntityRef]
        self._neighborhoods = dict(neighborhoods or {})  # node_id -> list[Neighbor]
        self._alias_matches = dict(alias_matches or {})  # alias.lower() -> list[AliasMatchNode]
        self.touched_since_arg = None

    async def get_node(self, node_id: str):
        return self.nodes.get(node_id)

    async def inbound_canonical_edges(self, node_id: str):
        return list(self._inbound.get(node_id, []))

    async def list_entities(self, *, types):
        return [e for e in self._entities if e.type in set(types)]

    async def entities_touched_since(self, *, types, since):
        self.touched_since_arg = since
        return [e for e in self._entities if e.type in set(types)]

    async def neighborhood(self, node_id: str):
        return list(self._neighborhoods.get(node_id, []))

    async def memory_nodes_matching_alias(self, alias, *, entity_id, window_start, limit):
        return list(self._alias_matches.get(alias.lower(), []))[:limit]


class FakeProfileStore:
    """In-memory profile store — records upserts + returns preset current hashes (profile tests)."""

    def __init__(self, *, hashes=None) -> None:
        self._hashes = dict(hashes or {})  # node_id -> neighborhood_hash
        self.upserts: list[dict] = []

    async def current_hash(self, node_id: str):
        return self._hashes.get(node_id)

    async def upsert_profile(
        self, node_id, *, tier, profile, observations, neighborhood_hash, embedding
    ) -> None:
        self.upserts.append(
            {
                "node_id": node_id,
                "tier": tier,
                "profile": profile,
                "observations": observations,
                "neighborhood_hash": neighborhood_hash,
                "embedding": embedding,
            }
        )
        self._hashes[node_id] = neighborhood_hash


class FakeAliasStore:
    """In-memory alias index for resolver tests. ``candidates_by_key`` maps a
    (normalized_name, type) to the exact-leg candidates a mention resolves against; ``entities`` is
    a flat pool of :class:`EntityCandidate` scanned by the **token-overlap** leg (ADR-040) when the
    resolver passes significant ``tokens``."""

    def __init__(
        self,
        *,
        candidates_by_key: dict[tuple[str, str], list[EntityCandidate]] | None = None,
        entities: list[EntityCandidate] | None = None,
    ) -> None:
        self._by_key = candidates_by_key or {}
        self._entities = list(entities or [])
        self.queries: list[tuple[str, tuple[str, ...]]] = []
        self.token_calls: list[tuple[str, ...]] = []

    async def find_candidates(
        self,
        name: str,
        *,
        types: list[str],
        tokens: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EntityCandidate]:
        self.queries.append((name, tuple(types)))
        self.token_calls.append(tuple(tokens or []))
        out: list[EntityCandidate] = []
        for t in types:
            out.extend(self._by_key.get((normalize_alias(name), t), []))
        if tokens:  # token-overlap leg: same-type hubs sharing a significant token
            tokset = set(tokens)
            seen = {c.id for c in out}
            for c in self._entities:
                if c.type not in set(types) or c.id in seen:
                    continue
                surf_tokens: set[str] = set()
                for s in [c.title or "", *c.aliases]:
                    surf_tokens.update(normalize_alias(s).split())
                if surf_tokens & tokset:
                    out.append(c)
                    seen.add(c.id)
        return out[:limit] if limit is not None else out


class FakeReviewQueue:
    """In-memory review queue — the write (``enqueue``) + read/resolve (``list_items``/``get``/
    ``resolve``) surfaces over one dict of rows, keyed by id. ``items`` keeps the filed
    :class:`ReviewItem`s (write-path assertions); ``records`` holds the full rows the read path
    returns. Mirrors the real store's guarded transition — decidable = ``pending`` ∪ ``maybe``
    (ADR-048 §7); ``resolved``/``discarded`` are terminal."""

    def __init__(self) -> None:
        self.items: list[ReviewItem] = []
        self.records: dict[str, ReviewRecord] = {}
        self._seq = 0

    async def enqueue(self, item: ReviewItem) -> str:
        self._seq += 1
        review_id = f"review-{self._seq}"
        self.items.append(item)
        self.records[review_id] = ReviewRecord(
            id=review_id,
            kind=item.kind,
            payload=dict(item.payload),
            excerpt=item.excerpt,
            source=item.source,
            source_ref=item.source_ref,
            status="pending",
            resolution=None,
            created_at=datetime.now(UTC),
        )
        return review_id

    async def list_items(
        self, *, status: str | None, kind: str | None, limit: int
    ) -> list[ReviewRecord]:
        rows = [
            r
            for r in self.records.values()
            if (status is None or r.status == status) and (kind is None or r.kind == kind)
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    async def get(self, review_id: str) -> ReviewRecord | None:
        return self.records.get(review_id)

    async def resolve(self, review_id: str, *, status: str, resolution: dict) -> bool:
        from dataclasses import replace

        row = self.records.get(review_id)
        if row is None or row.status not in ("pending", "maybe"):  # decidable ∪ maybe (ADR-048 §7)
            return False
        self.records[review_id] = replace(row, status=status, resolution=resolution)
        return True


class FakeVocabularyStore:
    """In-memory approved-vocabulary store — mirrors ``PgVocabularyStore`` (dedup, order-preserving,
    idempotent append) without a DB (ADR-027/035, M3 task 7)."""

    def __init__(self) -> None:
        self.node_types: list[str] = []
        self.edge_rels: list[str] = []
        self.entity_like_types: list[str] = []

    async def get_additions(self) -> VocabularyAdditions:
        return VocabularyAdditions(
            node_types=tuple(self.node_types),
            edge_rels=tuple(self.edge_rels),
            entity_like_types=tuple(self.entity_like_types),
        )

    async def add(
        self, *, node_types=(), edge_rels=(), entity_like_types=()
    ) -> VocabularyAdditions:
        for axis, incoming in (
            (self.node_types, node_types),
            (self.edge_rels, edge_rels),
            (self.entity_like_types, entity_like_types),
        ):
            for value in incoming:
                v = value.strip()
                if v and v not in axis:
                    axis.append(v)
        return await self.get_additions()


class FakeEdgeConsolidationStore:
    """In-memory EdgeConsolidationStore for edge-retro-consolidation tests — no live DB.

    ``candidates`` seeds the bounded edge inventory; ``paths`` maps a node id → its store path
    (apply resolves sources here). Records the ``exclude_rel``/``limit`` the propose passed so a
    test can assert the cap + the target-rel filter."""

    def __init__(
        self,
        *,
        candidates: list | None = None,
        paths: dict[str, str] | None = None,
    ) -> None:
        self._candidates = list(candidates or [])
        self._paths = dict(paths or {})
        self.inventory_args: dict | None = None
        self.store_paths_calls: list[list[str]] = []

    async def edge_inventory(self, *, exclude_rel: str, limit: int):
        self.inventory_args = {"exclude_rel": exclude_rel, "limit": limit}
        return [c for c in self._candidates if c.rel != exclude_rel][:limit]

    async def store_paths_for(self, node_ids: list[str]) -> dict[str, str]:
        self.store_paths_calls.append(list(node_ids))
        return {nid: self._paths[nid] for nid in node_ids if nid in self._paths}


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


class FakeChatStore:
    """In-memory ChatStore for chat-service tests — no live DB (08 testing policy).

    Sessions + messages live in dicts; ``add_message`` records the exact rows (so a test can assert
    the user turn was persisted before the model call, and that the assistant sources landed)."""

    def __init__(self) -> None:
        from app.chat.store import ChatMessageRecord, ChatSessionRecord

        self._ChatSessionRecord = ChatSessionRecord
        self._ChatMessageRecord = ChatMessageRecord
        self.sessions: dict[str, ChatSessionRecord] = {}
        self.messages: dict[str, list[ChatMessageRecord]] = {}
        self._seq = 0

    def _next(self, prefix: str) -> str:
        # Message ids stay human-readable; session ids must be real uuids to match the
        # ``chat_sessions.id`` uuid column (the router validates the id as a uuid at the boundary).
        self._seq += 1
        return f"{prefix}-{self._seq}"

    async def create_session(self, *, title: str | None = None) -> str:
        sid = str(uuid.uuid4())
        self.sessions[sid] = self._ChatSessionRecord(
            id=sid, title=title, created_at=datetime.now(UTC), last_model=None
        )
        self.messages[sid] = []
        return sid

    async def get_session(self, session_id: str):
        return self.sessions.get(session_id)

    async def list_sessions(self, limit: int):
        ordered = sorted(
            self.sessions.values(),
            key=lambda s: s.created_at or datetime.now(UTC),
            reverse=True,
        )
        return ordered[:limit]

    async def session_messages(self, session_id: str, *, limit: int | None = None):
        msgs = list(self.messages.get(session_id, []))
        return msgs[-limit:] if limit is not None else msgs

    async def add_message(
        self, session_id, *, role, content, model=None, sources=None
    ) -> str:
        mid = self._next("msg")
        self.messages.setdefault(session_id, []).append(
            self._ChatMessageRecord(
                id=mid,
                role=role,
                content=content,
                model=model,
                sources=list(sources or []),
                created_at=datetime.now(UTC),
            )
        )
        return mid

    async def set_title(self, session_id: str, title: str) -> None:
        from dataclasses import replace

        self.sessions[session_id] = replace(self.sessions[session_id], title=title)

    async def set_last_model(self, session_id: str, model: str) -> None:
        from dataclasses import replace

        self.sessions[session_id] = replace(self.sessions[session_id], last_model=model)


class FakeRetriever:
    """In-memory Retriever for chat-service tests. Returns preset hits and records the exact search
    arguments (query, top_k, planes, min_score) so a test can assert the chat floor + condensed
    query reached retrieval. ``down=True`` raises ProviderUnavailable (embedder-down path)."""

    def __init__(self, *, hits: list[SearchHit] | None = None, down: bool = False) -> None:
        self._hits = hits or []
        self._down = down
        self.calls: list[dict] = []

    async def search(
        self, query, *, top_k=None, planes=None, min_score=None
    ) -> list[SearchHit]:
        self.calls.append(
            {"query": query, "top_k": top_k, "planes": planes, "min_score": min_score}
        )
        if self._down:
            raise ProviderUnavailable("embedder down")
        return list(self._hits)


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
        # Mirror PgAgentRunStore: link to the ambient pipeline parent (ADR-047 §5) and register the
        # child so a PipelineRunner captures it — so the linkage is exercised identically in tests.
        self._seq += 1
        run_id = f"run-{self._seq}"
        self.runs[run_id] = AgentRun(
            id=run_id, agent=agent, status=RUNNING, parent_run_id=current_parent_run_id()
        )
        record_child_run(run_id)
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
        source: str | None = None,
        source_ref: str | None = None,
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
            source=source,
            source_ref=source_ref,
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


class FakeCapsuleStore:
    """In-memory CapsuleStore (M5 task 2): records the last saved blob, returns it from ``current``.

    ``raise_on_read`` flips ``current`` into a raising read so the build_context / chat best-effort
    (rule 7) paths can be exercised. Starts empty (``current`` → None) unless ``blob`` is preset."""

    def __init__(
        self, *, blob: CapsuleBlob | None = None, raise_on_read: bool = False
    ) -> None:
        self.blob = blob
        self.saved: list[CapsuleBlob] = []
        self.raise_on_read = raise_on_read

    async def current(self) -> CapsuleBlob | None:
        if self.raise_on_read:
            raise RuntimeError("capsule read boom")
        return self.blob

    async def save(self, blob: CapsuleBlob) -> None:
        self.saved.append(blob)
        self.blob = blob


class FakeChatDistillStore:
    """In-memory ChatDistillStore for chat-distiller tests — no live DB (08 testing policy).

    ``sessions`` is the preset distillable roster; ``messages`` maps a session id → its full message
    list (delta filtering by the ``after`` watermark is replicated so the service's delta handling
    is exercised). ``advanced`` records each watermark write so a test can assert skips."""

    def __init__(
        self,
        *,
        sessions=None,
        messages=None,
        known=None,
        watermarks=None,
    ) -> None:
        self._sessions = list(sessions or [])  # list[DistillableSession]
        self._messages = dict(messages or {})  # session_id -> list[ChatMessageRecord]
        # Which session ids exist (for `session_state` → None means 404). Defaults to the union of
        # the preset roster + any session with messages, so most tests need not pass it explicitly.
        self._known = set(known or set()) | set(self._messages) | {
            s.session_id for s in self._sessions
        }
        # Per-session watermark for the on-demand `session_state` path; `advance_watermark` updates
        # it in place so a remember-then-remember test sees the delta shrink (idempotency).
        self._watermarks: dict = dict(watermarks or {})
        for s in self._sessions:
            self._watermarks.setdefault(s.session_id, s.watermark)
        self.idle_cutoff_arg = None
        self.delta_calls: list[dict] = []
        self.advanced: list[dict] = []

    async def distillable_sessions(self, *, idle_cutoff, limit):
        self.idle_cutoff_arg = idle_cutoff
        return self._sessions[:limit]

    async def session_state(self, session_id):
        from app.chat.distill_store import SessionDistillState

        if session_id not in self._known:
            return None
        msgs = self._messages.get(session_id, [])
        newest = max(
            (m.created_at for m in msgs if m.created_at is not None), default=None
        )
        return SessionDistillState(
            session_id=session_id,
            watermark=self._watermarks.get(session_id),
            newest_at=newest,
        )

    async def delta_messages(self, session_id, *, after, limit):
        self.delta_calls.append({"session_id": session_id, "after": after, "limit": limit})
        msgs = [
            m
            for m in self._messages.get(session_id, [])
            if after is None or (m.created_at is not None and m.created_at > after)
        ]
        msgs.sort(key=lambda m: (m.created_at or datetime.now(UTC), m.id))
        return msgs[:limit] if limit is not None else msgs  # oldest-first (see PgChatDistillStore)

    async def advance_watermark(self, session_id, *, last_message_at, run_id):
        self.advanced.append(
            {"session_id": session_id, "last_message_at": last_message_at, "run_id": run_id}
        )
        self._watermarks[session_id] = last_message_at


class FakeChatCaptureIngest:
    """In-memory ChatCaptureIngest — records each endorsed candidate the distiller materializes as a
    ``source=chat`` capture (text + session id + the anchoring created_at). ``down`` raises so the
    best-effort path can be exercised."""

    def __init__(self, *, down: bool = False) -> None:
        self.captures: list[dict] = []
        self._down = down
        self._seq = 0

    async def create_chat_capture(self, text, *, session_id, created_at) -> str:
        if self._down:
            raise RuntimeError("ingest boom")
        self._seq += 1
        capture_id = f"cap-{self._seq}"
        self.captures.append(
            {
                "capture_id": capture_id,
                "text": text,
                "session_id": session_id,
                "created_at": created_at,
            }
        )
        return capture_id


class FakeAutoRecordedStore:
    """In-memory AutoRecordedStore (M6 task 4): records auto-endorsed capture ids + salience, tracks
    which are tombstoned. ``record`` is idempotent (ON CONFLICT DO NOTHING semantics)."""

    def __init__(self) -> None:
        self.recorded: dict[str, str | None] = {}  # capture_id → salience
        self.tombstoned: set[str] = set()
        self.list_calls: list[tuple[int, list[str]]] = []

    async def record(self, capture_id: str, *, salience: str | None) -> None:
        self.recorded.setdefault(capture_id, salience)

    async def is_recorded(self, capture_id: str) -> bool:
        return capture_id in self.recorded

    async def tombstone(self, capture_id: str) -> bool:
        if capture_id in self.tombstoned:
            return False
        self.tombstoned.add(capture_id)
        return True

    async def list_recent(self, limit, *, entity_types):
        self.list_calls.append((limit, list(entity_types)))
        return []


class FakeCapsuleSourceStore:
    """In-memory CapsuleSourceStore (M5 task 2): returns preset hubs/memories/insights, bounded by
    the requested limit so a test can assert the config caps are honored."""

    def __init__(
        self,
        *,
        hubs: list[HubProfile] | None = None,
        memories: list[RecentNode] | None = None,
        insights: list[RecentNode] | None = None,
    ) -> None:
        self._hubs = hubs or []
        self._memories = memories or []
        self._insights = insights or []
        self.limits: dict[str, int] = {}

    async def top_profile_hubs(self, limit: int) -> list[HubProfile]:
        self.limits["hubs"] = limit
        return self._hubs[:limit]

    async def recent_memories(self, limit: int) -> list[RecentNode]:
        self.limits["memories"] = limit
        return self._memories[:limit]

    async def recent_insights(self, limit: int) -> list[RecentNode]:
        self.limits["insights"] = limit
        return self._insights[:limit]
