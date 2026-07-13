"""The indexer service — the real index step (04-pipelines §4, ADR-022/026/030).

Turns graph-store node files into the derived index (``nodes`` + ``chunks`` + canonical ``edges``).
Per file:

    read → sha256 the WHOLE file ── unchanged (same id) ? skip / path-update
         → parse frontmatter (id/type/plane/planes/tags/aliases/disambig/occurred/edges/…)
         → strip frontmatter → chunk (02 §4) → batch-embed via nomic (``search_document:``)
         → per-node TRANSACTION: upsert node (keyed on id) + replace chunks + nodes.embedding
    then, in a second pass over the batch: materialize each node's canonical edges (frontmatter →
    edges table), so a target written in the same batch already exists (the dst_id FK).

Robustness (ADR-022): the per-node transaction means a node is never half-indexed; on an embed
failure the node is **skipped and the run continues** (existing rows left intact, run marked
**partial**), so a later reindex re-does only still-stale nodes (hash-skip). Indexing never
crashes its caller (rule 7). Derived ``similar`` edges are **not** computed here — they are a
nightly DB-only recompute over ``nodes.embedding`` (ADR-023 surviving half, ``graph/service.py``).

Identity is the frontmatter ``id`` (02 §3): a renamed file is a path update, not delete+insert.
``content_hash`` covers the whole file (no exclusions — the ``sb:related`` machinery is gone).
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..providers.base import ProviderUnavailable
from ..providers.registry import ProviderRegistry
from .chunking import chunk_node
from .frontmatter import NodeMetadata, parse_node_metadata
from .store import CanonicalEdge, IndexStore, NodeChunk, NodeUpsert

logger = logging.getLogger(__name__)

# nomic asymmetric task prefix for the indexing side (ADR-022); the search side uses
# ``search_query:``. The node title is prepended as context for each chunk (02 §4).
_DOCUMENT_PREFIX = "search_document:"


@dataclass(frozen=True)
class IndexOutcome:
    """Result of an index run over a set of paths (feeds the ``reindex`` agent_runs row later)."""

    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    edges: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        """True when at least one node was skipped by an embed failure (run status ``partial``)."""
        return self.failed > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "indexed": self.indexed,
            "skipped": self.skipped,
            "failed": self.failed,
            "deleted": self.deleted,
            "edges": self.edges,
            "partial": self.partial,
            "failures": list(self.failures),
        }


class NodeIndexer(Protocol):
    """The indexing surface the capture/ingestion pipelines depend on."""

    async def index_paths(self, store_paths: list[str]) -> IndexOutcome: ...


class Indexer:
    """Indexes graph-store node files into ``nodes`` + ``chunks`` + canonical ``edges``."""

    def __init__(
        self, *, settings: Settings, store: IndexStore, registry: ProviderRegistry
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._root = Path(settings.graph_store_path)
        self._ignore = set(settings.store_ignore)

    async def index_paths(self, store_paths: list[str]) -> IndexOutcome:
        """Index a specific set of nodes (the capture/ingestion write path), then materialize their
        canonical edges in a second pass so same-batch targets already exist (the dst_id FK).

        Skip-and-continue: a per-node failure (missing file, embed unavailable) is logged and
        counted, never raised — the rest of the batch still indexes.
        """
        indexed = skipped = failed = 0
        failures: list[str] = []
        pending_edges: list[tuple[str, list[CanonicalEdge]]] = []
        for store_path in store_paths:
            try:
                result, meta = await self._index_one(store_path)
            except Exception:  # noqa: BLE001 — one bad node must not abort the batch (rule 7)
                logger.exception("index: unexpected error on %s, skipping", store_path)
                result, meta = _Result.FAILED, None
            if result is _Result.INDEXED and meta is not None:
                indexed += 1
                pending_edges.append((meta.id, _canonical_edges(meta)))
            elif result is _Result.SKIPPED:
                skipped += 1
            else:
                failed += 1
                failures.append(store_path)

        edges = 0
        for node_id, node_edges in pending_edges:
            edges += await self._store.replace_canonical_edges(node_id, node_edges)

        return IndexOutcome(
            indexed=indexed, skipped=skipped, failed=failed, edges=edges, failures=failures
        )

    async def reindex_all(self) -> IndexOutcome:
        """Full rescan: index every store ``*.md`` and reconcile deletions (04 §4).

        DB rows whose files no longer exist are removed (``chunks``/``edges`` cascade); a rename is
        an id-keyed path update (not delete+insert). Idempotent. Does **not** recompute the derived
        ``similar`` edges — that is a nightly-only DB step (ADR-023), layered on by the caller.
        """
        paths = await asyncio.to_thread(self._scan_store)
        outcome = await self.index_paths(paths)
        stale = await self._store.list_indexed_paths() - set(paths)
        deleted = await self._store.delete_nodes(sorted(stale))
        if deleted:
            logger.info("reindex removed %d node(s) whose store files are gone", deleted)
        return IndexOutcome(
            indexed=outcome.indexed,
            skipped=outcome.skipped,
            failed=outcome.failed,
            deleted=deleted,
            edges=outcome.edges,
            failures=outcome.failures,
        )

    @staticmethod
    def content_hash(raw_text: str) -> str:
        """sha256 over the WHOLE file (02 §3) — no exclusions (the ``sb:related`` machinery that
        needed a carve-out is gone). Newlines are normalized to LF so a CRLF checkout (git on
        Windows) doesn't shift the hash. Any content or frontmatter edit reindexes."""
        normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    # --- per-node core ------------------------------------------------------------------

    async def _index_one(self, store_path: str) -> tuple[_Result, NodeMetadata | None]:
        read = await asyncio.to_thread(self._read_node, store_path)
        if read is None:
            logger.warning("index: node %s missing on disk, skipping", store_path)
            return _Result.FAILED, None
        raw_text, mtime = read

        meta = parse_node_metadata(raw_text, store_path=store_path, fallback_created=mtime)
        content_hash = self.content_hash(raw_text)
        state = await self._store.get_index_state(meta.id)
        if state is not None and state.content_hash == content_hash:
            if state.store_path != store_path:
                # A moved-but-unchanged file: update only the path (id-keyed, no re-embed, 04 §4).
                await self._store.update_node_path(meta.id, store_path)
            return _Result.SKIPPED, meta

        chunks = chunk_node(
            raw_text,
            chunk_size=self._settings.chunk_size,
            chunk_overlap=self._settings.chunk_overlap,
        )
        node_chunks: list[NodeChunk] = []
        node_embedding: list[float] | None = None
        if chunks:
            try:
                vectors = await self._embed_chunks(chunks, title=meta.title)
            except ProviderUnavailable as exc:
                # Skip-and-continue (ADR-022): leave any existing rows intact, mark the run
                # partial. A later reindex retries this still-stale node.
                logger.warning("index: embed failed for %s (%s); skipping", store_path, exc)
                return _Result.FAILED, None
            node_chunks = [
                NodeChunk(index=i, content=chunk, embedding=vector)
                for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
            ]
            node_embedding = _mean_pool(vectors)

        await self._store.upsert_node(
            NodeUpsert(
                id=meta.id,
                store_path=store_path,
                type=meta.type,
                content_hash=content_hash,
                title=meta.title,
                plane=meta.plane,
                planes=meta.planes,
                tags=meta.tags,
                aliases=meta.aliases,
                disambig=meta.disambig,
                occurred_start=meta.occurred_start,
                occurred_end=meta.occurred_end,
                organizer_version=meta.organizer_version,
                merged_into=meta.merged_into,
                source=meta.source,
                source_ref=meta.source_ref,
                node_created_at=meta.created,
                embedding=node_embedding,
                chunks=node_chunks,
            )
        )
        return _Result.INDEXED, meta

    async def _embed_chunks(self, chunks: list[str], *, title: str | None) -> list[list[float]]:
        """Batch-embed a node's chunks in one call, with bounded 429/unavailable backoff.

        Each chunk is prefixed ``search_document: {title}\\n\\n{chunk}`` — the asymmetric nomic
        task prefix is mandatory (ADR-022) and the title gives each chunk node-level context (02
        §4). One call per node; the whole node's chunks share the retry.
        """
        prefix = f"{_DOCUMENT_PREFIX} {title or ''}"
        inputs = [f"{prefix}\n\n{chunk}" for chunk in chunks]
        attempts = max(1, self._settings.embed_max_attempts)
        for attempt in range(attempts):
            try:
                result = await self._registry.embed(inputs)
                return result.vectors
            except ProviderUnavailable:
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(self._settings.embed_retry_backoff_seconds * (2**attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    # --- store filesystem ---------------------------------------------------------------

    def _read_node(self, store_path: str) -> tuple[str, datetime] | None:
        """Read a node's text + mtime (used as the ``created`` fallback). ``None`` if it's gone."""
        path = self._root / Path(*store_path.split("/"))
        try:
            raw_text = path.read_text(encoding="utf-8")
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return raw_text, mtime

    def _scan_store(self) -> list[str]:
        """Every indexable ``*.md`` as a ``/``-separated store-relative path (02 §1).

        Files under a ``STORE_IGNORE`` segment (``.trash``, ``.git``, ``templates``) are skipped
        at any depth.
        """
        if not self._root.exists():
            return []
        paths: list[str] = []
        for abs_path in self._root.rglob("*.md"):
            if not abs_path.is_file():
                continue
            rel_parts = abs_path.relative_to(self._root).parts
            if self._ignore.intersection(rel_parts):
                continue
            paths.append("/".join(rel_parts))
        return sorted(paths)


class _Result(enum.Enum):
    """Per-node index outcome (internal — callers see the aggregated :class:`IndexOutcome`)."""

    INDEXED = enum.auto()
    SKIPPED = enum.auto()
    FAILED = enum.auto()


def _canonical_edges(meta: NodeMetadata) -> list[CanonicalEdge]:
    """A node's parsed frontmatter edges → the store's :class:`CanonicalEdge` shape (score=conf)."""
    return [
        CanonicalEdge(dst_id=e.to, rel=e.rel, score=e.conf, since=e.since, until=e.until)
        for e in meta.edges
    ]


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    """Element-wise mean of the chunk vectors → the node embedding (ADR-023; no extra call)."""
    count = len(vectors)
    dim = len(vectors[0])
    sums = [0.0] * dim
    for vector in vectors:
        for i, value in enumerate(vector):
            sums[i] += value
    return [value / count for value in sums]
