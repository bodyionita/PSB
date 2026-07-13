"""DerivedEdgeGraph tests — fake graph store, no live DB (08 testing policy).

The pivot (ADR-026) made derived ``similar`` edges **DB-only**: no file rendering, no ``sb:related``
block, no commit step, no churn gate. The service just recomputes the neighbour set and replaces
``edges(origin='derived')`` wholesale. These tests cover that the tuned top-K/floor reach the store
and that the computed edges are the ones materialized.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.graph.service import DerivedEdgeGraph
from app.graph.store import SimilarEdge

from .fakes import FakeGraphStore


def _make(tmp_path: Path, *, store: FakeGraphStore, top_k: int = 5, min_score: float = 0.5):
    settings = Settings(
        graph_store_path=str(tmp_path / "store"),
        similar_top_k=top_k,
        similar_min_score=min_score,
    )
    return DerivedEdgeGraph(settings=settings, store=store)


async def test_recompute_materializes_the_computed_edges(tmp_path: Path):
    edges = [
        SimilarEdge(src_id="id-a", dst_id="id-b", score=0.7),
        SimilarEdge(src_id="id-b", dst_id="id-a", score=0.7),
    ]
    store = FakeGraphStore(edges=edges)
    graph = _make(tmp_path, store=store)

    outcome = await graph.recompute()

    assert store.written == edges  # the derived edges were replaced with the computed set
    assert outcome.edges == 2


async def test_tuned_top_k_and_floor_reach_the_store(tmp_path: Path):
    store = FakeGraphStore(edges=[])
    graph = _make(tmp_path, store=store, top_k=3, min_score=0.42)

    outcome = await graph.recompute()

    assert store.compute_args == {"top_k": 3, "min_score": 0.42}
    assert outcome.edges == 0  # empty graph ⇒ nothing written
    assert store.written == []
