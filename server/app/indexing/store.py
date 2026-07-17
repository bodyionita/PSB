"""Persistence for the derived graph index (02-data-model §3, ADR-026/030/031).

The indexer depends on the :class:`IndexStore` *protocol*, not on asyncpg, so it unit-tests
against an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgIndexStore` is the
plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011).

Two writes:
  * :meth:`upsert_node` — one **per-node transaction** that replaces a node's row and *all* its
    chunks atomically (02 §3, ADR-022), keyed on the frontmatter **``id``** (paths are
    projections — a moved file is a path update, not delete+insert). A node is never half-indexed.
  * :meth:`replace_canonical_edges` — materializes the node's frontmatter edges into the ``edges``
    table (``origin='canonical'``), run in a second pass so a target written in the same batch
    already exists (the ``dst_id`` FK). Edges to a still-unknown target are skipped and reconciled
    on the next reindex.

Embeddings pass as plain Python float lists: the ``vector`` codec registered on the asyncpg pool
(``db.py``) encodes them, and the column type enforces the 768 dimension (migration 005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class NodeChunk:
    """One retrieval chunk destined for the ``chunks`` table."""

    index: int
    content: str
    embedding: list[float]


@dataclass(frozen=True)
class CanonicalEdge:
    """A frontmatter edge to materialize (02 §2). ``score`` = confidence (``conf``; ``None`` ⇒
    1.0 semantics); ``since``/``until`` are the validity window (ADR-030/032)."""

    dst_id: str
    rel: str
    score: float | None = None
    since: date | None = None
    until: date | None = None


@dataclass(frozen=True)
class IndexState:
    """A node's indexed ``(content_hash, store_path)`` — drives hash-skip + move detection."""

    content_hash: str
    store_path: str


@dataclass(frozen=True)
class NodeUpsert:
    """A fully-prepared ``nodes`` row plus its chunks — the unit of a per-node transaction (02 §3).

    Keyed on ``id`` (the frontmatter identity). ``embedding`` is the node-level mean-pool vector
    (``None`` when the node produced no chunks); ``chunks`` may be empty for an empty node (the row
    is still tracked so its hash skips it next time).
    """

    id: str
    store_path: str
    type: str
    content_hash: str
    title: str | None = None
    plane: str | None = None
    planes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    disambig: str | None = None
    occurred_start: date | None = None
    occurred_end: date | None = None
    interiority: str | None = None
    organizer_version: str | None = None
    merged_into: str | None = None
    source: str | None = None
    source_ref: str | None = None
    node_created_at: datetime | None = None
    embedding: list[float] | None = None
    chunks: list[NodeChunk] = field(default_factory=list)


class IndexStore(Protocol):
    """The index persistence surface the indexer relies on."""

    async def get_index_state(self, node_id: str) -> IndexState | None:
        """Current ``(content_hash, store_path)`` for a node id, or ``None`` if not indexed yet."""
        ...

    async def upsert_node(self, node: NodeUpsert) -> None:
        """Replace a node's row + all its chunks in one transaction, keyed on ``id`` (delete old
        chunks, upsert the node, insert the new chunks). Idempotent."""
        ...

    async def update_node_path(self, node_id: str, store_path: str) -> None:
        """A moved-but-unchanged file: update only ``store_path`` (no re-embed); id-keyed."""
        ...

    async def replace_canonical_edges(self, node_id: str, edges: list[CanonicalEdge]) -> int:
        """Replace a node's ``origin='canonical'`` out-edges with ``edges``, dropping any whose
        target node does not exist. Returns the number of edges written."""
        ...

    async def list_indexed_paths(self) -> set[str]:
        """Every ``store_path`` currently in the index (for deletion reconciliation on rescan)."""
        ...

    async def delete_nodes(self, store_paths: list[str]) -> int:
        """Delete nodes by path (``chunks``/``edges`` cascade). Returns the row count."""
        ...


class PgIndexStore:
    """asyncpg-backed index store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_index_state(self, node_id: str) -> IndexState | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content_hash, store_path FROM nodes WHERE id = $1", node_id
            )
        if row is None:
            return None
        return IndexState(content_hash=row["content_hash"], store_path=row["store_path"])

    async def upsert_node(self, node: NodeUpsert) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO nodes (
                    id, store_path, type, title, plane, planes, tags, aliases, disambig,
                    occurred_start, occurred_end, organizer_version, merged_into,
                    source, source_ref, content_hash, embedding, node_created_at, interiority,
                    indexed_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                        $17, $18, $19, now())
                ON CONFLICT (id) DO UPDATE SET
                    store_path = EXCLUDED.store_path,
                    type = EXCLUDED.type,
                    title = EXCLUDED.title,
                    plane = EXCLUDED.plane,
                    planes = EXCLUDED.planes,
                    tags = EXCLUDED.tags,
                    aliases = EXCLUDED.aliases,
                    disambig = EXCLUDED.disambig,
                    occurred_start = EXCLUDED.occurred_start,
                    occurred_end = EXCLUDED.occurred_end,
                    organizer_version = EXCLUDED.organizer_version,
                    merged_into = EXCLUDED.merged_into,
                    source = EXCLUDED.source,
                    source_ref = EXCLUDED.source_ref,
                    content_hash = EXCLUDED.content_hash,
                    embedding = EXCLUDED.embedding,
                    node_created_at = EXCLUDED.node_created_at,
                    interiority = EXCLUDED.interiority,
                    indexed_at = now()
                """,
                node.id,
                node.store_path,
                node.type,
                node.title,
                node.plane,
                node.planes,
                node.tags,
                node.aliases,
                node.disambig,
                node.occurred_start,
                node.occurred_end,
                node.organizer_version,
                node.merged_into,
                node.source,
                node.source_ref,
                node.content_hash,
                node.embedding,
                node.node_created_at,
                node.interiority,
            )
            # Replace the chunk set wholesale (a node is re-chunked on every reindex).
            await conn.execute("DELETE FROM chunks WHERE node_id = $1", node.id)
            if node.chunks:
                await conn.executemany(
                    """
                    INSERT INTO chunks (node_id, chunk_index, content, embedding)
                    VALUES ($1, $2, $3, $4)
                    """,
                    [(node.id, c.index, c.content, c.embedding) for c in node.chunks],
                )

    async def update_node_path(self, node_id: str, store_path: str) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                "UPDATE nodes SET store_path = $2, indexed_at = now() WHERE id = $1",
                node_id,
                store_path,
            )

    async def replace_canonical_edges(self, node_id: str, edges: list[CanonicalEdge]) -> int:
        async with self._db.transaction() as conn:
            await conn.execute(
                "DELETE FROM edges WHERE src_id = $1 AND origin = 'canonical'", node_id
            )
            if not edges:
                return 0
            # Only materialize edges whose target node exists (the dst_id FK) — a dangling target
            # is skipped and reconciled when it appears (04 §3). Dedup on (dst, rel) so a repeated
            # frontmatter edge can't violate the (src, dst, rel, origin) pk.
            existing = {
                str(r["id"])
                for r in await conn.fetch(
                    "SELECT id FROM nodes WHERE id = ANY($1::uuid[])",
                    [e.dst_id for e in edges],
                )
            }
            rows: list[tuple[str, str, str, float | None, date | None, date | None]] = []
            seen: set[tuple[str, str]] = set()
            for e in edges:
                key = (e.dst_id, e.rel)
                if e.dst_id not in existing or key in seen:
                    continue
                seen.add(key)
                rows.append((node_id, e.dst_id, e.rel, e.score, e.since, e.until))
            if rows:
                await conn.executemany(
                    """
                    INSERT INTO edges (src_id, dst_id, rel, origin, score, since, until)
                    VALUES ($1, $2, $3, 'canonical', $4, $5, $6)
                    """,
                    rows,
                )
            return len(rows)

    async def list_indexed_paths(self) -> set[str]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch("SELECT store_path FROM nodes")
        return {row["store_path"] for row in rows}

    async def delete_nodes(self, store_paths: list[str]) -> int:
        if not store_paths:
            return 0
        async with self._db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM nodes WHERE store_path = ANY($1::text[])", store_paths
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0
