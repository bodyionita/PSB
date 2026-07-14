"""Persistence for edge retro-consolidation (ADR-036 / M3 task 7b).

The ``POST /admin/vocab/consolidate`` propose reads a **bounded inventory of existing canonical
edges** (source + target titles, a short source excerpt, and the current rel) to feed the distill
chain; apply resolves the chosen edges' source store paths so the writer can rewrite them. Both
reads come from the derived ``nodes``/``edges``/``chunks`` index (a cache of the graph store —
rule 1), never the files.

As elsewhere, callers depend on the :class:`EdgeConsolidationStore` *protocol* so they unit-test
against an in-memory fake (no live DB in CI — 08 testing policy); :class:`PgEdgeConsolidationStore`
is the plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011). Tombstoned endpoints
(``merged_into`` set) are excluded so a merged node is never a candidate or a target (ADR-030 §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class EdgeCandidate:
    """One existing canonical edge offered to the re-walk: source + target identity, current rel,
    and a short source excerpt for the LLM to judge whether the new rel fits better."""

    src_id: str
    src_title: str | None
    src_excerpt: str | None
    rel: str
    dst_id: str
    dst_title: str | None


class EdgeConsolidationStore(Protocol):
    """The two reads the edge-consolidation service needs (bounded inventory + path resolution)."""

    async def edge_inventory(self, *, exclude_rel: str, limit: int) -> list[EdgeCandidate]:
        """Up to ``limit`` existing canonical edges whose rel is **not** ``exclude_rel`` (an edge
        already using the target rel can't be re-typed onto it), live endpoints only, **most-recent
        source first** so the cap is a recency window over a large graph (ADR-036 §1)."""
        ...

    async def store_paths_for(self, node_ids: list[str]) -> dict[str, str]:
        """Map live (non-tombstone) node ids → their store paths (apply resolves sources here rather
        than trusting a client-supplied path)."""
        ...


class PgEdgeConsolidationStore:
    """asyncpg-backed edge-inventory reads — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def edge_inventory(self, *, exclude_rel: str, limit: int) -> list[EdgeCandidate]:
        if limit <= 0:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.src_id, s.title AS src_title, e.rel, e.dst_id, d.title AS dst_title,
                       c.content AS src_excerpt
                FROM edges e
                JOIN nodes s ON s.id = e.src_id
                JOIN nodes d ON d.id = e.dst_id
                LEFT JOIN chunks c ON c.node_id = e.src_id AND c.chunk_index = 0
                WHERE e.origin = 'canonical' AND e.rel <> $1
                  AND s.merged_into IS NULL AND d.merged_into IS NULL
                ORDER BY s.node_created_at DESC NULLS LAST, e.src_id, e.rel, e.dst_id
                LIMIT $2
                """,
                exclude_rel,
                limit,
            )
        return [
            EdgeCandidate(
                src_id=str(r["src_id"]),
                src_title=r["src_title"],
                src_excerpt=r["src_excerpt"],
                rel=r["rel"],
                dst_id=str(r["dst_id"]),
                dst_title=r["dst_title"],
            )
            for r in rows
        ]

    async def store_paths_for(self, node_ids: list[str]) -> dict[str, str]:
        if not node_ids:
            return {}
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, store_path FROM nodes
                WHERE id = ANY($1::uuid[]) AND merged_into IS NULL
                """,
                list(node_ids),
            )
        return {str(r["id"]): r["store_path"] for r in rows}
