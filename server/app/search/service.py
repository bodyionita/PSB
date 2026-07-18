"""Search service — node-grouped semantic search + read-only node detail (03-api §Search & graph).

Embeds the query with the **mandatory** ``search_query:`` nomic prefix (ADR-022 — the asymmetric
counterpart of the indexer's ``search_document:``), delegates the cosine ranking to the store, and
trims each hit's best chunk to a snippet. ``get_node`` reads the node **body from the store file**
(fidelity — it reflects any hand edits, not the indexed snapshot) and attaches the node's edges
(canonical + derived, both directions).

The derived entity **profile** ([ADR-030](adr/030-entity-substrate-and-lifecycle.md)) is served
here too, read from ``node_profiles`` in the same query; it is ``None`` for content nodes and for
entities the nightly profile-refresh job (M3 task 6) hasn't reached yet.

No LLM call beyond the single query embedding; a down embedder surfaces as ``ProviderUnavailable``
for the router to map to ``503`` (this is a request path, not a background job).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..config import Settings
from ..indexing.chunking import split_frontmatter
from ..providers.registry import ProviderRegistry
from ..services.node_media_store import NodeMediaItem
from .store import NodeEdgeView, RetrievalParams, SearchHit, SearchStore

# nomic asymmetric task prefix for the query side (ADR-022); the indexer uses ``search_document:``.
_QUERY_PREFIX = "search_query:"


@dataclass(frozen=True)
class NodePreview:
    """A node's metadata + store-file body + edges + derived profile (GET /nodes/{id})."""

    node_id: str
    store_path: str
    type: str
    title: str | None
    plane: str | None
    planes: list[str]
    tags: list[str]
    aliases: list[str]
    disambig: str | None
    occurred: date | None
    occurred_end: date | None
    body: str
    profile: str | None
    edges: list[NodeEdgeView]
    merged_into: str | None
    created_at: datetime | None = None
    interiority: str | None = None
    # The media this node carries (M9 T4, ADR-060 §1) — the `GET /nodes/{id}.media[]` strip.
    media: list[NodeMediaItem] = field(default_factory=list)


class SearchService:
    def __init__(
        self, *, settings: Settings, store: SearchStore, registry: ProviderRegistry
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._root = Path(settings.graph_store_path)

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        planes: list[str] | None = None,
        types: list[str] | None = None,
        since: date | None = None,
        until: date | None = None,
        as_of: date | None = None,
        min_score: float | None = None,
        interiority_boost: float | None = None,
    ) -> list[SearchHit]:
        """Hybrid (vector ⊍ FTS, RRF-fused) node-grouped search + recency prior (03-api §Search).

        The **same** query string drives both legs: the vector leg embeds it (``search_query:``
        prefix, ADR-022); the FTS leg passes it verbatim to ``websearch_to_tsquery`` in the store.
        ``since``/``until``/``as_of`` are the M4 temporal filters. ``min_score`` overrides the
        configured floor on the fused RRF×recency score — chat passes its own tuned floor for the
        "not in your memories" cut (04 §5, MINOR-1); ``None`` uses ``search_min_score``. Raises
        ``ProviderUnavailable`` if the query can't be embedded (single embedding provider, no hot
        fallback — ADR-022).
        """
        limit = self._clamp_top_k(top_k)
        result = await self._registry.embed([f"{_QUERY_PREFIX} {query}"])
        params = RetrievalParams(
            top_k=limit,
            # Fuse at least `top_k` candidates from each leg, else a large top_k could out-run the
            # configured pool and silently truncate before ranking.
            candidates=max(self._settings.search_rrf_candidates, limit),
            rrf_k=self._settings.search_rrf_k,
            recency_half_life_days=self._settings.search_recency_half_life_days,
            recency_floor=self._settings.search_recency_floor,
            min_score=(min_score if min_score is not None else self._settings.search_min_score),
            # 1.0 = neutral (ADR-055 §3a): `/search` never boosts; only chat passes a value.
            interiority_boost=(interiority_boost if interiority_boost is not None else 1.0),
            planes=planes or None,  # an empty filter list means "no filter"
            types=types or None,
            since=since,
            until=until,
            as_of=as_of,
        )
        hits = await self._store.search_chunks(result.vectors[0], query, params)
        return [self._trim_snippet(hit) for hit in hits]

    async def get_node(self, node_id: str) -> NodePreview | None:
        """Read-only detail: stored metadata + edges + the current store-file body (03-api)."""
        row = await self._store.get_node(node_id)
        if row is None:
            return None
        body = await asyncio.to_thread(self._read_body, row.store_path)
        return NodePreview(
            node_id=row.node_id,
            store_path=row.store_path,
            type=row.type,
            title=row.title,
            plane=row.plane,
            planes=row.planes,
            tags=row.tags,
            aliases=row.aliases,
            disambig=row.disambig,
            occurred=row.occurred_start,
            occurred_end=row.occurred_end,
            body=body,
            profile=row.profile,  # derived entity profile (node_profiles, ADR-030 §4 / task 6)
            edges=row.edges,
            merged_into=row.merged_into,
            created_at=row.created_at,
            interiority=row.interiority,
            media=row.media,  # node↔media link (M9 T4, ADR-060 §1)
        )

    def _clamp_top_k(self, top_k: int | None) -> int:
        requested = top_k if top_k is not None else self._settings.search_top_k_default
        return max(1, min(requested, self._settings.search_max_top_k))

    def _trim_snippet(self, hit: SearchHit) -> SearchHit:
        limit = self._settings.search_snippet_max_chars
        if len(hit.snippet) <= limit:
            return hit
        trimmed = hit.snippet[:limit].rstrip() + "…"
        return SearchHit(
            node_id=hit.node_id,
            store_path=hit.store_path,
            type=hit.type,
            title=hit.title,
            plane=hit.plane,
            planes=hit.planes,
            tags=hit.tags,
            snippet=trimmed,
            score=hit.score,
            occurred_start=hit.occurred_start,
            occurred_end=hit.occurred_end,
            created_at=hit.created_at,
            interiority=hit.interiority,
            media_kinds=hit.media_kinds,
        )

    def _read_body(self, store_path: str) -> str:
        """Body from the store file (frontmatter stripped). ``""`` if the file is gone (stale row
        pre-reconciliation) — the preview degrades rather than 500s."""
        path = self._root / Path(*store_path.split("/"))
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return ""
        _, body = split_frontmatter(raw_text)
        return body.strip()
