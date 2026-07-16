"""Dedup-sweep DB reads (02-data-model §3, ADR-049 §3–§6) — the candidate SQL, the re-file guard,
and the survivor-degree read the nightly :class:`~app.dedup.sweep.DedupSweepService` composes over.

Plain SQL over asyncpg (rule 5, ADR-011); the service depends on the :class:`DedupStore` *protocol*
so it unit-tests against a fake (no live DB in CI — 08 testing policy). :class:`PgDedupStore` is the
implementation. Every read excludes tombstones (``merged_into`` set) on both ends.

The candidate query mirrors ``DerivedEdgeGraph.compute_similar`` (an HNSW top-K LATERAL per recent
node) so the work is index-bounded, then applies the ADR-049 §3 **strict AND** gate in SQL: high
cosine **and** a shared canonical edge to a common entity hub **and** occurred-overlap (a null
``occurred_start`` on either side never excludes). ``vector`` codec is not needed — every embedding
reference is a column, evaluated server-side against the HNSW index (as in the derived recompute).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class DedupCandidate:
    """One qualifying near-duplicate pair from the sweep's candidate scan (directional: ``node_a``
    is the recent driver, ``node_b`` its neighbour — the sweep canonicalizes the pair afterward).

    ``shared_entity_ids``/``titles`` are the common entity hubs both nodes link (the §3 gate
    evidence, shown in the review payload); ``occurred_overlap`` is True only when **both** nodes
    are dated and their windows genuinely overlap (an undated pair passes the gate but is not a
    date signal — ADR-049 §3). ``title_a``/``title_b`` feed the review item's human excerpt."""

    node_a: str
    node_b: str
    cosine: float
    shared_entity_ids: list[str]
    shared_entity_titles: list[str]
    occurred_overlap: bool
    title_a: str | None
    title_b: str | None


@dataclass(frozen=True)
class NodeMergeStat:
    """A node's default-survivor inputs (ADR-049 §6): its canonical degree (in+out, derived edges
    excluded as transient noise) + its creation/index times for the age tiebreak."""

    node_id: str
    degree: int
    node_created_at: datetime | None
    indexed_at: datetime | None


class DedupStore(Protocol):
    """The dedup-read surface the sweep service relies on (candidates + guard + survivor stats)."""

    async def candidate_pairs(
        self,
        *,
        content_types: list[str],
        entity_like_types: list[str],
        watermark: datetime,
        min_cosine: float,
        candidate_k: int,
    ) -> list[DedupCandidate]:
        """Recent content nodes' near-duplicate neighbours passing the strict-AND gate (ADR-049 §3).

        Only the *driver* is recency-bounded (``indexed_at >= watermark``); the neighbour may be any
        content node (a new node often dups an old one). Ordered by cosine desc so the run's cap
        keeps the strongest dups when it truncates."""
        ...

    async def proposal_exists(self, node_a: str, node_b: str) -> bool:
        """True if a ``dedup-proposal`` in **any** status already carries this canonical pair
        (``payload.node_a``/``node_b``) — the re-file guard so a decided pair is never re-proposed
        (ADR-049 §5). ``node_a``/``node_b`` must be the canonical ``least→greatest`` ids."""
        ...

    async def survivor_stats(self, node_ids: list[str]) -> dict[str, NodeMergeStat]:
        """Canonical degree + creation/index time for each id, for the default-survivor pick."""
        ...


class PgDedupStore:
    """asyncpg-backed dedup reads — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def candidate_pairs(
        self,
        *,
        content_types: list[str],
        entity_like_types: list[str],
        watermark: datetime,
        min_cosine: float,
        candidate_k: int,
    ) -> list[DedupCandidate]:
        if not content_types or not entity_like_types:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH recent AS (
                    SELECT id, embedding, occurred_start, occurred_end, title
                    FROM nodes
                    WHERE type = ANY($1::text[]) AND merged_into IS NULL
                      AND embedding IS NOT NULL AND indexed_at >= $3
                ),
                cand AS (
                    SELECT n.id AS a_id, m.id AS b_id,
                           1 - (m.embedding <=> n.embedding) AS cosine,
                           n.title AS a_title, m.title AS b_title,
                           n.occurred_start AS a_os, n.occurred_end AS a_oe,
                           m.occurred_start AS b_os, m.occurred_end AS b_oe
                    FROM recent n
                    CROSS JOIN LATERAL (
                        SELECT id, embedding, occurred_start, occurred_end, title
                        FROM nodes m
                        WHERE m.id <> n.id AND m.type = ANY($1::text[])
                          AND m.embedding IS NOT NULL AND m.merged_into IS NULL
                        ORDER BY m.embedding <=> n.embedding, m.id
                        LIMIT $4
                    ) m
                    WHERE 1 - (m.embedding <=> n.embedding) >= $5
                      -- occurred-overlap gate (ADR-049 §3): exclude only when BOTH dated AND
                      -- disjoint; a null occurred_start on either side never excludes.
                      AND (n.occurred_start IS NULL OR m.occurred_start IS NULL
                           OR (n.occurred_start <= COALESCE(m.occurred_end, m.occurred_start)
                               AND m.occurred_start <= COALESCE(n.occurred_end, n.occurred_start)))
                )
                SELECT c.a_id, c.b_id, c.cosine, c.a_title, c.b_title,
                       (c.a_os IS NOT NULL AND c.b_os IS NOT NULL
                        AND c.a_os <= COALESCE(c.b_oe, c.b_os)
                        AND c.b_os <= COALESCE(c.a_oe, c.a_os)) AS occurred_overlap,
                       s.entity_ids, s.entity_titles
                FROM cand c
                CROSS JOIN LATERAL (
                    SELECT array_agg(e.id ORDER BY e.id) AS entity_ids,
                           array_agg(e.title ORDER BY e.id) AS entity_titles
                    FROM (
                        SELECT DISTINCT ent.id::text AS id, ent.title
                        FROM edges ea
                        JOIN edges eb ON eb.dst_id = ea.dst_id
                                     AND eb.src_id = c.b_id AND eb.origin = 'canonical'
                        JOIN nodes ent ON ent.id = ea.dst_id
                        WHERE ea.src_id = c.a_id AND ea.origin = 'canonical'
                          AND ent.type = ANY($2::text[]) AND ent.merged_into IS NULL
                    ) e
                ) s
                WHERE s.entity_ids IS NOT NULL  -- >=1 shared entity hub (array_agg of none is NULL)
                ORDER BY c.cosine DESC, c.a_id, c.b_id
                """,
                content_types,
                entity_like_types,
                watermark,
                candidate_k,
                min_cosine,
            )
        return [
            DedupCandidate(
                node_a=str(r["a_id"]),
                node_b=str(r["b_id"]),
                cosine=float(r["cosine"]),
                shared_entity_ids=[str(x) for x in (r["entity_ids"] or [])],
                shared_entity_titles=[x for x in (r["entity_titles"] or [])],
                occurred_overlap=bool(r["occurred_overlap"]),
                title_a=r["a_title"],
                title_b=r["b_title"],
            )
            for r in rows
        ]

    async def proposal_exists(self, node_a: str, node_b: str) -> bool:
        async with self._db.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT 1 FROM review_queue
                WHERE kind = 'dedup-proposal'
                  AND payload->>'node_a' = $1 AND payload->>'node_b' = $2
                LIMIT 1
                """,
                node_a,
                node_b,
            )
        return row is not None

    async def survivor_stats(self, node_ids: list[str]) -> dict[str, NodeMergeStat]:
        if not node_ids:
            return {}
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT n.id, n.node_created_at, n.indexed_at,
                       (SELECT count(*) FROM edges e
                         WHERE e.origin = 'canonical'
                           AND (e.src_id = n.id OR e.dst_id = n.id)) AS degree
                FROM nodes n
                WHERE n.id = ANY($1::uuid[])
                """,
                node_ids,
            )
        return {
            str(r["id"]): NodeMergeStat(
                node_id=str(r["id"]),
                degree=int(r["degree"]),
                node_created_at=r["node_created_at"],
                indexed_at=r["indexed_at"],
            )
            for r in rows
        }
