"""Persistence for the graph edges (02-data-model §3): the derived ``similar``-edge recompute
(ADR-023 surviving half) and the one-hop **neighbor read** primitive (M5 task 1, ADR-046/028/032).

Each service depends on a *protocol*, not on asyncpg, so both unit-test against in-memory fakes (no
live DB in CI — 08 testing policy). :class:`PgGraphStore` / :class:`PgNeighborStore` are the
plain-SQL asyncpg implementations (CLAUDE.md rule 5, ADR-011).

**Derived edges.** Neighbours are computed **in the database** via pgvector: for each node, its
top-K nearest other nodes over ``nodes.embedding`` cosine, filtered to the ``SIMILAR_MIN_SCORE``
floor, materialized as ``edges(origin='derived', rel='similar')`` — **DB-only, no file rendering**
(ADR-026 deleted the ``sb:related`` block). The ``vector`` codec on the pool (``db.py``) is not
needed there — both operands of ``<=>`` are columns, evaluated server-side against the HNSW index.

**Neighbor read** (:class:`NeighborStore`). A center node's 1-hop edges, both directions and both
origins, **keyset-paginated** for a finite LLM context: MCP ``traverse`` and the M7 map endpoint
share it. Tombstoned nodes (``merged_into`` set) are excluded from **both** ends throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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


# The keyset a neighbor page resumes after: the ``(origin, rel, dir, node_id)`` of the last row a
# prior page returned. Matches the store's ``ORDER BY`` exactly (the four columns, in order) so the
# next page is every row strictly greater — stable even as edges are added/removed between calls.
NeighborCursor = tuple[str, str, str, str]


@dataclass(frozen=True)
class NeighborEdge:
    """One 1-hop neighbor of a center node: the connecting edge + the *other* endpoint (M5 task 1).

    ``dir`` is ``out`` (center → other) or ``in`` (other → center); ``origin`` is ``canonical`` |
    ``derived``; ``score`` is confidence (canonical, ``None`` ⇒ 1.0) or cosine (derived). The
    endpoint's ``type``/``title``/``plane`` ride along so a caller renders a neighbor without a
    second fetch (M7 map uses ``plane`` for colour, ``type`` for shape)."""

    origin: str
    rel: str
    dir: str
    node_id: str
    type: str | None
    title: str | None
    plane: str | None
    score: float | None
    since: date | None
    until: date | None


class NeighborStore(Protocol):
    """The one-hop neighbor-read surface (MCP ``traverse`` + M7 map + ``build_context``)."""

    async def neighbors(
        self,
        node_id: str,
        *,
        rel: str | None,
        direction: str | None,
        after: NeighborCursor | None,
        limit: int,
    ) -> list[NeighborEdge]:
        """A page of ``node_id``'s 1-hop neighbors, ordered by ``(origin, rel, dir, node_id)``.

        ``rel`` filters to one relation; ``direction`` (``out``/``in``, ``None`` = both) picks the
        edge direction; ``after`` resumes strictly past a prior page's last key; at most ``limit``
        rows are returned. Tombstoned endpoints are excluded. The caller asks for one more than it
        needs to detect a further page (keyset pagination)."""
        ...


class PgNeighborStore:
    """asyncpg-backed one-hop neighbor reads — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def neighbors(
        self,
        node_id: str,
        *,
        rel: str | None,
        direction: str | None,
        after: NeighborCursor | None,
        limit: int,
    ) -> list[NeighborEdge]:
        # Both directions are unioned then filtered/ordered/paged as one set, so a single keyset
        # walks the whole neighborhood regardless of edge direction. The `out` leg joins the dst
        # endpoint, the `in` leg the src; each excludes a tombstoned *other* endpoint (the center
        # itself may be a live-resolving tombstone — that's a get_node concern, not here). Keyset:
        # `(origin, rel, dir, node_id) > (after…)` matches the ORDER BY, so "next page" is every row
        # strictly after the last one returned. The whole `after` tuple is NULL on the first page
        # (the `$4::text IS NULL` guard short-circuits); node_id is compared as uuid.
        ao, ar, ad, an = after if after is not None else (None, None, None, None)
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT origin, rel, dir, node_id, type, title, plane, score, since, until
                FROM (
                    SELECT e.origin, e.rel, 'out' AS dir, e.dst_id AS node_id,
                           n.type, n.title, n.plane, e.score, e.since, e.until
                    FROM edges e JOIN nodes n ON n.id = e.dst_id
                    WHERE e.src_id = $1 AND n.merged_into IS NULL
                    UNION ALL
                    SELECT e.origin, e.rel, 'in' AS dir, e.src_id AS node_id,
                           n.type, n.title, n.plane, e.score, e.since, e.until
                    FROM edges e JOIN nodes n ON n.id = e.src_id
                    WHERE e.dst_id = $1 AND n.merged_into IS NULL
                ) nbr
                WHERE ($2::text IS NULL OR rel = $2)
                  AND ($3::text IS NULL OR dir = $3)
                  AND ($4::text IS NULL
                       OR (origin, rel, dir, node_id) > ($4, $5, $6, $7::uuid))
                ORDER BY origin, rel, dir, node_id
                LIMIT $8
                """,
                node_id,
                rel,
                direction,
                ao,
                ar,
                ad,
                an,
                limit,
            )
        return [
            NeighborEdge(
                origin=r["origin"],
                rel=r["rel"],
                dir=r["dir"],
                node_id=str(r["node_id"]),
                type=r["type"],
                title=r["title"],
                plane=r["plane"],
                score=float(r["score"]) if r["score"] is not None else None,
                since=r["since"],
                until=r["until"],
            )
            for r in rows
        ]
