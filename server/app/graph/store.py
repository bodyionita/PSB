"""Persistence for the derived ``similar`` edges (02-data-model §3, ADR-023 surviving half).

The derived-edge service depends on the :class:`GraphStore` *protocol*, not on asyncpg, so it
unit-tests against an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgGraphStore`
is the plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011).

Neighbours are computed **in the database** via pgvector: for each node, its top-K nearest other
nodes over ``nodes.embedding`` cosine, filtered to the ``SIMILAR_MIN_SCORE`` floor. The result is
materialized as ``edges(origin='derived', rel='similar')`` — **DB-only, no file rendering**
(ADR-026 deleted the ``sb:related`` block). Tombstoned nodes (``merged_into`` set) are excluded
from both ends. The ``vector`` codec on the pool (``db.py``) is not needed here — both operands of
``<=>`` are columns, so the distance is evaluated server-side against the HNSW index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class SimilarEdge:
    """One derived neighbour: a directional ``src → dst`` edge with its cosine score."""

    src_id: str
    dst_id: str
    score: float


class GraphStore(Protocol):
    """The persistence surface the derived-edge service relies on."""

    async def compute_similar(self, *, top_k: int, min_score: float) -> list[SimilarEdge]:
        """Per node, its top-K nearest other nodes above ``min_score`` (cosine), as directional
        edges. Tombstoned nodes are excluded; nodes without an embedding are ignored."""
        ...

    async def replace_derived_edges(self, edges: list[SimilarEdge]) -> int:
        """Replace all ``origin='derived'`` edges with these, in one transaction. Returns the
        number of edge rows written."""
        ...


class PgGraphStore:
    """asyncpg-backed graph store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def compute_similar(self, *, top_k: int, min_score: float) -> list[SimilarEdge]:
        # Per source node n, a LATERAL picks its top_k nearest other nodes by cosine distance; the
        # outer WHERE drops any below the floor. score = 1 - cosine distance. The distance is
        # written ``m.embedding <=> n.embedding`` (indexed column on the left) so the planner can
        # use the HNSW index on ``nodes.embedding``. ``m.id`` is a deterministic tiebreaker so
        # exact-score ties can't reorder between runs. Tombstones (merged_into) are excluded both
        # sides — a merged node is a redirect, hidden from search/map (ADR-030 §5).
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT n.id AS src_id, m.id AS dst_id, 1 - (m.embedding <=> n.embedding) AS score
                FROM nodes n
                CROSS JOIN LATERAL (
                    SELECT id, embedding
                    FROM nodes m
                    WHERE m.id <> n.id AND m.embedding IS NOT NULL AND m.merged_into IS NULL
                    ORDER BY m.embedding <=> n.embedding, m.id
                    LIMIT $1
                ) m
                WHERE n.embedding IS NOT NULL AND n.merged_into IS NULL
                  AND 1 - (m.embedding <=> n.embedding) >= $2
                ORDER BY n.id, score DESC, m.id
                """,
                top_k,
                min_score,
            )
        return [
            SimilarEdge(src_id=str(r["src_id"]), dst_id=str(r["dst_id"]), score=float(r["score"]))
            for r in rows
        ]

    async def replace_derived_edges(self, edges: list[SimilarEdge]) -> int:
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM edges WHERE origin = 'derived'")
            if edges:
                await conn.executemany(
                    """
                    INSERT INTO edges (src_id, dst_id, rel, origin, score)
                    VALUES ($1, $2, 'similar', 'derived', $3)
                    """,
                    [(e.src_id, e.dst_id, e.score) for e in edges],
                )
        return len(edges)
