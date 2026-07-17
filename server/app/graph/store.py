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
    # The endpoint's inner-voice dimension (M8.2 T3.5, ADR-055 §3c): `internal`|`external`|`mixed`,
    # or None on an unstamped entity hub — the map's inner-voice mark, no second fetch. Trailing +
    # defaulted so existing NeighborEdge call sites (MCP render, tests) are unaffected.
    interiority: str | None = None


@dataclass(frozen=True)
class NeighborHeader:
    """A center node's render header for the map — no body, no edges (M7, 03-api §Nodes neighbors).

    Just the fields the grouped ``GET /nodes/{id}/neighbors`` echoes back as ``center`` so the
    canvas can label/colour the focal node without the heavier ``get_node`` file read."""

    node_id: str
    type: str
    title: str | None
    plane: str | None
    planes: list[str]
    # The center's own inner-voice dimension (M8.2 T3.5, ADR-055 §3c) so the focal node is markable.
    # Trailing + defaulted so existing NeighborHeader call sites (tests) are unaffected.
    interiority: str | None = None


@dataclass(frozen=True)
class ZonedNeighbor:
    """One neighbor of a center, tagged with its ``rel`` zone's full size (M7 grouped, ADR-052).

    ``edge`` is the neighbor itself (capped to the zone fanout by the query); ``zone_total`` is the
    count of *all* neighbors in this ``rel`` zone (both origins for ``similar``) — feeds "show N of
    M". Zones are keyed by ``rel`` alone; the neighbor's own ``origin`` carries the styling."""

    edge: NeighborEdge
    zone_total: int


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

    async def center_header(self, node_id: str) -> NeighborHeader | None:
        """The center node's render header (type/title/plane/planes), or ``None`` if unknown.

        A live-resolving tombstone still returns its header (the map re-centers on live nodes; the
        302 redirect is ``get_node``'s concern) — a lightweight read for the ``center`` echo, not a
        detail view."""
        ...

    async def neighbor_zones(
        self,
        node_id: str,
        *,
        direction: str | None,
        fanout: int,
    ) -> list[ZonedNeighbor]:
        """All of ``node_id``'s ``rel`` zones, each capped to the first ``fanout`` rows (ADR-052).

        Rows are ordered by ``(rel, origin, dir, node_id)`` (so consecutive rows share a rel zone;
        canonical before derived within a rel) and each carries its zone's full ``zone_total``
        (unaffected by the cap) for the "show more". ``direction`` (``out``/``in``, ``None`` = both)
        scopes both the neighbors and the totals. Tombstoned endpoints are excluded (both ends);
        ``until``-closed edges are returned."""
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
                SELECT origin, rel, dir, node_id, type, title, plane, score, since, until,
                       interiority
                FROM (
                    SELECT e.origin, e.rel, 'out' AS dir, e.dst_id AS node_id,
                           n.type, n.title, n.plane, e.score, e.since, e.until, n.interiority
                    FROM edges e JOIN nodes n ON n.id = e.dst_id
                    WHERE e.src_id = $1 AND n.merged_into IS NULL
                    UNION ALL
                    SELECT e.origin, e.rel, 'in' AS dir, e.src_id AS node_id,
                           n.type, n.title, n.plane, e.score, e.since, e.until, n.interiority
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
        return [_neighbor_edge(r) for r in rows]

    async def center_header(self, node_id: str) -> NeighborHeader | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, type, title, plane, planes, interiority FROM nodes WHERE id = $1",
                node_id,
            )
        if row is None:
            return None
        return NeighborHeader(
            node_id=str(row["id"]),
            type=row["type"],
            title=row["title"],
            plane=row["plane"],
            planes=list(row["planes"] or []),
            interiority=row["interiority"],
        )

    async def neighbor_zones(
        self,
        node_id: str,
        *,
        direction: str | None,
        fanout: int,
    ) -> list[ZonedNeighbor]:
        # The same both-directions union as `neighbors`, but window-ranked per `rel` zone (ADR-052:
        # zones are keyed by rel, so the sole dual-origin rel `similar` — canonical link + derived
        # recompute — is one zone). ROW_NUMBER caps each zone to its first `fanout` neighbors and
        # COUNT(*) OVER carries its true size, both PARTITION BY rel. The rank order is
        # `(origin, dir, node_id)` — the M5 keyset `(origin, rel, dir, node_id)` with rel fixed —
        # so canonical surfaces before derived and the per-zone next_cursor resumes the rel-only
        # keyset exactly. The direction filter sits in `ranked`'s WHERE, which SQL evaluates before
        # the window functions, so both the ranking and the count are direction-scoped. The outer
        # ORDER BY leads with `rel` so each zone's rows are contiguous (the service groups by rel).
        # One round-trip returns every zone already capped — bounded regardless of a hub's degree.
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT origin, rel, dir, node_id, type, title, plane, score, since, until,
                       interiority, zone_total
                FROM (
                    SELECT origin, rel, dir, node_id, type, title, plane, score, since, until,
                           interiority,
                           ROW_NUMBER() OVER (
                               PARTITION BY rel ORDER BY origin, dir, node_id) AS rn,
                           COUNT(*) OVER (PARTITION BY rel) AS zone_total
                    FROM (
                        SELECT e.origin, e.rel, 'out' AS dir, e.dst_id AS node_id,
                               n.type, n.title, n.plane, e.score, e.since, e.until, n.interiority
                        FROM edges e JOIN nodes n ON n.id = e.dst_id
                        WHERE e.src_id = $1 AND n.merged_into IS NULL
                        UNION ALL
                        SELECT e.origin, e.rel, 'in' AS dir, e.src_id AS node_id,
                               n.type, n.title, n.plane, e.score, e.since, e.until, n.interiority
                        FROM edges e JOIN nodes n ON n.id = e.src_id
                        WHERE e.dst_id = $1 AND n.merged_into IS NULL
                    ) nbr
                    WHERE ($2::text IS NULL OR dir = $2)
                ) ranked
                WHERE rn <= $3
                ORDER BY rel, origin, dir, node_id
                """,
                node_id,
                direction,
                fanout,
            )
        return [
            ZonedNeighbor(edge=_neighbor_edge(r), zone_total=int(r["zone_total"])) for r in rows
        ]


def _neighbor_edge(r) -> NeighborEdge:
    """Build a :class:`NeighborEdge` from an asyncpg row carrying the neighbor columns
    (ten + ``interiority``, M8.2 T3.5)."""
    return NeighborEdge(
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
        interiority=r["interiority"],
    )
