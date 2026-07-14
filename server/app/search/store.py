"""Read side of the derived index (03-api §Search & graph, 02 §3).

The search service depends on the :class:`SearchStore` *protocol*, not on asyncpg, so it unit-tests
against an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgSearchStore` is the
plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011).

Query embeddings pass as plain ``list[float]`` — the ``vector`` codec on the pool (see ``db.py``)
encodes them for the ``<=>`` cosine operator, which runs against the HNSW index (migration 005).
Tombstoned nodes (``merged_into`` set) are hidden from search and from neighbour lists (ADR-030 §5).

**M4 hybrid retrieval** ([ADR-032](adr/032-prior-art-adoptions.md) §5/§7): ``search_chunks`` fuses a
**vector** leg (cosine over ``chunks`` ⊍ ``node_profiles.embedding``, best-per-node — ADR-037) with
a **full-text** leg (``tsvector`` over the same ``chunks`` ⊍ ``node_profiles`` set, migration 008)
by **Reciprocal Rank Fusion** (rank-based, ``k=60`` — never blend raw cosine with ``ts_rank``), then
applies a **mild recency prior** on ``occurred ?? created``. The FTS leg self-suppresses on
non-English / zero-lexeme queries (the English corpus contains no matching lexemes — no
language-detect dependency). Temporal filters (``since``/``until``/``as_of``) narrow both legs.
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
class RetrievalParams:
    """Tuning + filters for one hybrid retrieval (built by the service from Settings + request).

    Grouped into a value object so the store signature stays legible as the retriever gains knobs.
    ``candidates`` is the per-leg pool taken before RRF; ``rrf_k`` is the fusion constant; the two
    ``recency_*`` fields shape the multiplicative prior; ``min_score`` floors the final fused score.
    ``planes``/``types``/``since``/``until``/``as_of`` = ``None`` skips that filter."""

    top_k: int
    candidates: int
    rrf_k: int
    recency_half_life_days: float
    recency_floor: float
    min_score: float
    planes: list[str] | None = None
    types: list[str] | None = None
    since: date | None = None
    until: date | None = None
    as_of: date | None = None


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
        self, embedding: list[float], query_text: str, params: RetrievalParams
    ) -> list[SearchHit]:
        """Node-grouped hybrid search: one row per node, ranked by fused RRF×recency score desc.

        Fuses a vector leg (``embedding``, cosine) with a full-text leg (``query_text`` → tsvector)
        by RRF, applies the recency prior, and returns the top ``params.top_k`` above
        ``params.min_score``. Tombstoned nodes are excluded; the filters in ``params`` narrow both
        legs. The FTS leg contributes nothing when ``query_text`` yields no matching lexemes."""
        ...

    async def get_node(self, node_id: str) -> NodeRow | None:
        """A node's metadata + its canonical/derived edges (both directions), or ``None``."""
        ...


class PgSearchStore:
    """asyncpg-backed read store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def search_chunks(
        self, embedding: list[float], query_text: str, params: RetrievalParams
    ) -> list[SearchHit]:
        # HYBRID RRF (ADR-032 §5/§7). Two legs, each a best-per-node union over `chunks` ⊍
        # `node_profiles` (ADR-037: same node universe, so a chunk hit and a profile hit for one
        # node collapse to a single candidate):
        #   • VEC — cosine distance `<=>` over `chunks.embedding` ⊍ `node_profiles.embedding`;
        #           best (nearest) row per node, then the top `candidates` by distance.
        #   • FTS — `tsvector @@ websearch_to_tsquery('english', $4)` over `chunks.tsv` ⊍
        #           `node_profiles.tsv` (migration 008); best `ts_rank` per node, top `candidates`.
        # Each leg is rank-numbered (row_number, 1 = best) and the two are FULL-OUTER-JOINed on
        # node_id. RRF fuses by RANK, never by raw score — cosine and ts_rank are incommensurate:
        #   rrf = 1/(k + vec_rank) + 1/(k + fts_rank)   (a leg missing the node contributes 0).
        # A mild recency prior multiplies the fused score (bounded [floor,1], never zeroes an old
        # node): factor = floor + (1-floor)·0.5^(age_days / half_life), age on `occurred_start ??
        # node_created_at`, future dates clamped to age 0 → factor 1. The FTS leg self-suppresses on
        # non-English / empty queries (no matching lexemes → 0 rows → vector-only ranking). Filters
        # (planes/types + temporal) apply to every sub-leg; a `$…::T IS NULL` skips that filter.
        # Temporal: `since`/`until` = occurred-range overlap on occurred_start/occurred_end; `as_of`
        # = node-date `occurred_start <= as_of` (ADR-032; undated nodes fall outside any window).
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH vec AS (
                    SELECT node_id, store_path, type, title, plane, planes, tags,
                           occurred_start, node_created_at, snippet,
                           row_number() OVER (ORDER BY dist ASC, node_id) AS rank
                    FROM (
                        SELECT DISTINCT ON (node_id)
                            node_id, store_path, type, title, plane, planes, tags,
                            occurred_start, node_created_at, snippet, dist
                        FROM (
                            SELECT n.id AS node_id, n.store_path, n.type, n.title, n.plane,
                                   n.planes, n.tags, n.occurred_start, n.node_created_at,
                                   c.content AS snippet, c.embedding <=> $1 AS dist
                            FROM chunks c JOIN nodes n ON n.id = c.node_id
                            WHERE c.embedding IS NOT NULL AND n.merged_into IS NULL
                              AND ($2::text[] IS NULL OR n.planes && $2::text[])
                              AND ($3::text[] IS NULL OR n.type = ANY($3::text[]))
                              AND ($5::date IS NULL
                                   OR COALESCE(n.occurred_end, n.occurred_start) >= $5)
                              AND ($6::date IS NULL OR n.occurred_start <= $6)
                              AND ($7::date IS NULL OR n.occurred_start <= $7)
                            UNION ALL
                            SELECT n.id, n.store_path, n.type, n.title, n.plane, n.planes, n.tags,
                                   n.occurred_start, n.node_created_at,
                                   np.profile AS snippet, np.embedding <=> $1 AS dist
                            FROM node_profiles np JOIN nodes n ON n.id = np.node_id
                            WHERE np.embedding IS NOT NULL AND n.merged_into IS NULL
                              AND ($2::text[] IS NULL OR n.planes && $2::text[])
                              AND ($3::text[] IS NULL OR n.type = ANY($3::text[]))
                              AND ($5::date IS NULL
                                   OR COALESCE(n.occurred_end, n.occurred_start) >= $5)
                              AND ($6::date IS NULL OR n.occurred_start <= $6)
                              AND ($7::date IS NULL OR n.occurred_start <= $7)
                        ) legs
                        ORDER BY node_id, dist
                    ) best
                    ORDER BY dist ASC, node_id
                    LIMIT $8
                ),
                fts AS (
                    SELECT node_id, store_path, type, title, plane, planes, tags,
                           occurred_start, node_created_at, snippet,
                           row_number() OVER (ORDER BY ftrank DESC, node_id) AS rank
                    FROM (
                        SELECT DISTINCT ON (node_id)
                            node_id, store_path, type, title, plane, planes, tags,
                            occurred_start, node_created_at, snippet, ftrank
                        FROM (
                            SELECT n.id AS node_id, n.store_path, n.type, n.title, n.plane,
                                   n.planes, n.tags, n.occurred_start, n.node_created_at,
                                   c.content AS snippet,
                                   ts_rank(c.tsv, websearch_to_tsquery('english', $4)) AS ftrank
                            FROM chunks c JOIN nodes n ON n.id = c.node_id
                            WHERE c.tsv @@ websearch_to_tsquery('english', $4)
                              AND n.merged_into IS NULL
                              AND ($2::text[] IS NULL OR n.planes && $2::text[])
                              AND ($3::text[] IS NULL OR n.type = ANY($3::text[]))
                              AND ($5::date IS NULL
                                   OR COALESCE(n.occurred_end, n.occurred_start) >= $5)
                              AND ($6::date IS NULL OR n.occurred_start <= $6)
                              AND ($7::date IS NULL OR n.occurred_start <= $7)
                            UNION ALL
                            SELECT n.id, n.store_path, n.type, n.title, n.plane, n.planes, n.tags,
                                   n.occurred_start, n.node_created_at, np.profile AS snippet,
                                   ts_rank(np.tsv, websearch_to_tsquery('english', $4)) AS ftrank
                            FROM node_profiles np JOIN nodes n ON n.id = np.node_id
                            WHERE np.tsv @@ websearch_to_tsquery('english', $4)
                              AND n.merged_into IS NULL
                              AND ($2::text[] IS NULL OR n.planes && $2::text[])
                              AND ($3::text[] IS NULL OR n.type = ANY($3::text[]))
                              AND ($5::date IS NULL
                                   OR COALESCE(n.occurred_end, n.occurred_start) >= $5)
                              AND ($6::date IS NULL OR n.occurred_start <= $6)
                              AND ($7::date IS NULL OR n.occurred_start <= $7)
                        ) legs
                        ORDER BY node_id, ftrank DESC
                    ) best
                    ORDER BY ftrank DESC, node_id
                    LIMIT $8
                ),
                fused AS (
                    SELECT
                        COALESCE(v.node_id, f.node_id)       AS node_id,
                        COALESCE(v.store_path, f.store_path) AS store_path,
                        COALESCE(v.type, f.type)             AS type,
                        COALESCE(v.title, f.title)           AS title,
                        COALESCE(v.plane, f.plane)           AS plane,
                        COALESCE(v.planes, f.planes)         AS planes,
                        COALESCE(v.tags, f.tags)             AS tags,
                        COALESCE(v.snippet, f.snippet)       AS snippet,
                        COALESCE(v.occurred_start, f.occurred_start)   AS occurred_start,
                        COALESCE(v.node_created_at, f.node_created_at) AS node_created_at,
                        COALESCE(1.0 / ($9 + v.rank), 0.0)
                          + COALESCE(1.0 / ($9 + f.rank), 0.0) AS rrf
                    FROM vec v FULL OUTER JOIN fts f ON v.node_id = f.node_id
                ),
                scored AS (
                    -- `today` is pinned to UTC (CLAUDE convention: DB timestamps are UTC) so the
                    -- recency age doesn't drift by a day under a non-UTC session timezone.
                    SELECT node_id, store_path, type, title, plane, planes, tags, snippet,
                           rrf * ($11::float + (1 - $11::float) * power(
                               0.5,
                               GREATEST(
                                   (now() AT TIME ZONE 'UTC')::date
                                   - COALESCE(occurred_start, node_created_at::date,
                                              (now() AT TIME ZONE 'UTC')::date),
                                   0
                               )::float / $10::float
                           )) AS score
                    FROM fused
                )
                SELECT node_id, store_path, type, title, plane, planes, tags, snippet, score
                FROM scored
                WHERE score >= $12
                ORDER BY score DESC
                LIMIT $13
                """,
                embedding,
                params.planes,
                params.types,
                query_text,
                params.since,
                params.until,
                params.as_of,
                params.candidates,
                params.rrf_k,
                params.recency_half_life_days,
                params.recency_floor,
                params.min_score,
                params.top_k,
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
