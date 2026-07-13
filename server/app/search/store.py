"""Read side of the derived index (03-api §Search & graph, 02 §3).

The search service depends on the :class:`SearchStore` *protocol*, not on asyncpg, so it unit-tests
against an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgSearchStore` is the
plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011).

Query embeddings pass as plain ``list[float]`` — the ``vector`` codec on the pool (see ``db.py``)
encodes them for the ``<=>`` cosine operator, which runs against the HNSW index (migration 005).
Tombstoned nodes (``merged_into`` set) are hidden from search and from neighbour lists (ADR-030 §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class SearchHit:
    """One node in a search result — its best-scoring chunk supplies the snippet + score."""

    node_id: str
    store_path: str
    type: str
    title: str | None
    plane: str | None
    planes: list[str]
    tags: list[str]
    snippet: str
    score: float


@dataclass(frozen=True)
class NodeEdgeView:
    """One edge of a node for the detail view (03-api §Nodes): the *other* endpoint + edge meta.

    ``dir`` is ``out`` (this node → other) or ``in`` (other → this node); ``origin`` is
    ``canonical`` | ``derived``; ``score`` is confidence (canonical) or cosine (derived)."""

    rel: str
    dir: str
    node_id: str
    type: str | None
    title: str | None
    origin: str
    score: float | None
    since: date | None
    until: date | None


@dataclass(frozen=True)
class NodeRow:
    """A node's stored metadata + derived profile + its edges (body read from the store separately).

    ``profile`` is the derived entity profile (``node_profiles``, ADR-030 §4) — ``None`` for content
    nodes and for entities the profile-refresh job hasn't reached yet."""

    node_id: str
    store_path: str
    type: str
    title: str | None
    plane: str | None
    planes: list[str]
    tags: list[str]
    aliases: list[str]
    disambig: str | None
    occurred_start: date | None
    occurred_end: date | None
    merged_into: str | None
    profile: str | None = None
    edges: list[NodeEdgeView] = field(default_factory=list)


class SearchStore(Protocol):
    """The read surface the search service relies on."""

    async def search_chunks(
        self,
        embedding: list[float],
        *,
        top_k: int,
        planes: list[str] | None,
        types: list[str] | None,
        min_score: float,
    ) -> list[SearchHit]:
        """Node-grouped cosine search: one row per node (best chunk), ranked by score desc.
        Tombstoned nodes are excluded; ``planes``/``types`` = None skips that filter."""
        ...

    async def get_node(self, node_id: str) -> NodeRow | None:
        """A node's metadata + its canonical/derived edges (both directions), or ``None``."""
        ...


class PgSearchStore:
    """asyncpg-backed read store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def search_chunks(
        self,
        embedding: list[float],
        *,
        top_k: int,
        planes: list[str] | None,
        types: list[str] | None,
        min_score: float,
    ) -> list[SearchHit]:
        # Best chunk per node via DISTINCT ON (node, ascending cosine distance), then re-rank the
        # per-node winners by score and take top_k. `planes && $2` is array-overlap membership
        # (ADR-005 — never folder); `n.type = ANY($3)` filters node type; `$2/$3 IS NULL` skips the
        # filter. Tombstones excluded. score = 1 - cosine distance.
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT node_id, store_path, type, title, plane, planes, tags, snippet, score
                FROM (
                    SELECT DISTINCT ON (n.id)
                        n.id          AS node_id,
                        n.store_path  AS store_path,
                        n.type        AS type,
                        n.title       AS title,
                        n.plane       AS plane,
                        n.planes      AS planes,
                        n.tags        AS tags,
                        c.content     AS snippet,
                        1 - (c.embedding <=> $1) AS score
                    FROM chunks c
                    JOIN nodes n ON n.id = c.node_id
                    WHERE c.embedding IS NOT NULL
                      AND n.merged_into IS NULL
                      AND ($2::text[] IS NULL OR n.planes && $2::text[])
                      AND ($3::text[] IS NULL OR n.type = ANY($3::text[]))
                    ORDER BY n.id, c.embedding <=> $1
                ) best
                WHERE score >= $4
                ORDER BY score DESC
                LIMIT $5
                """,
                embedding,
                planes,
                types,
                min_score,
                top_k,
            )
        return [
            SearchHit(
                node_id=str(row["node_id"]),
                store_path=row["store_path"],
                type=row["type"],
                title=row["title"],
                plane=row["plane"],
                planes=list(row["planes"] or []),
                tags=list(row["tags"] or []),
                snippet=row["snippet"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def get_node(self, node_id: str) -> NodeRow | None:
        async with self._db.acquire() as conn:
            node = await conn.fetchrow(
                """
                SELECT n.id, n.store_path, n.type, n.title, n.plane, n.planes, n.tags, n.aliases,
                       n.disambig, n.occurred_start, n.occurred_end, n.merged_into, np.profile
                FROM nodes n
                LEFT JOIN node_profiles np ON np.node_id = n.id
                WHERE n.id = $1
                """,
                node_id,
            )
            if node is None:
                return None
            edges = await conn.fetch(
                """
                SELECT e.rel, 'out' AS dir, e.dst_id AS other_id, n2.type AS other_type,
                       n2.title AS other_title, e.origin, e.score, e.since, e.until
                FROM edges e JOIN nodes n2 ON n2.id = e.dst_id
                WHERE e.src_id = $1 AND n2.merged_into IS NULL
                UNION ALL
                SELECT e.rel, 'in' AS dir, e.src_id AS other_id, n2.type AS other_type,
                       n2.title AS other_title, e.origin, e.score, e.since, e.until
                FROM edges e JOIN nodes n2 ON n2.id = e.src_id
                WHERE e.dst_id = $1 AND n2.merged_into IS NULL
                ORDER BY origin, rel
                """,
                node_id,
            )
        return NodeRow(
            node_id=str(node["id"]),
            store_path=node["store_path"],
            type=node["type"],
            title=node["title"],
            plane=node["plane"],
            planes=list(node["planes"] or []),
            tags=list(node["tags"] or []),
            aliases=list(node["aliases"] or []),
            disambig=node["disambig"],
            occurred_start=node["occurred_start"],
            occurred_end=node["occurred_end"],
            merged_into=str(node["merged_into"]) if node["merged_into"] else None,
            profile=node["profile"],
            edges=[
                NodeEdgeView(
                    rel=e["rel"],
                    dir=e["dir"],
                    node_id=str(e["other_id"]),
                    type=e["other_type"],
                    title=e["other_title"],
                    origin=e["origin"],
                    score=float(e["score"]) if e["score"] is not None else None,
                    since=e["since"],
                    until=e["until"],
                )
                for e in edges
            ],
        )
