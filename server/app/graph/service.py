"""Derived ``similar``-edge recompute (ADR-023 surviving half, retargeted at M3).

One entry point, :meth:`DerivedEdgeGraph.recompute`, does the whole wholesale rebuild:

    top-K over nodes.embedding cosine above SIMILAR_MIN_SCORE
      → replace edges(origin='derived', rel='similar')   [DB-only]

The graph is **global** (adding one node can shift others' neighbours), so it is recomputed as a
whole — nightly, and on ``POST /admin/reindex`` (wired by the reindex task) — never on the
real-time capture write. Unlike the pre-pivot relatedness graph, this is **DB-only**: there is no
file rendering, no ``sb:related`` block, no commit step, no churn-gating (all deleted by
[ADR-026](adr/026-graph-native-storage-obsidian-removed.md)). Canonical edges are materialized
separately, from frontmatter, by the indexer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from .store import GraphStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphOutcome:
    """Result of a derived-edge recompute (feeds the ``reindex`` agent_runs details later)."""

    edges: int = 0  # derived `similar` edge rows written

    def as_dict(self) -> dict[str, object]:
        return {"edges": self.edges}


class DerivedEdgeGraph:
    """Recomputes the ``edges(origin='derived')`` neighbour set (ADR-023 surviving half)."""

    def __init__(self, *, settings: Settings, store: GraphStore) -> None:
        self._store = store
        self._top_k = settings.similar_top_k
        self._min_score = settings.similar_min_score

    async def recompute(self) -> GraphOutcome:
        """Full wholesale recompute of the derived edges. Never partial (ADR-023). DB-only."""
        edges = await self._store.compute_similar(top_k=self._top_k, min_score=self._min_score)
        written = await self._store.replace_derived_edges(edges)
        logger.info("derived-edge recompute: %d similar edge(s) written", written)
        return GraphOutcome(edges=written)
