"""Dedup-sweep tests (M6 task 5, ADR-049) — the nightly near-duplicate sweep that files
``dedup-proposal`` review items. Exercised against fakes (dedup store, review queue, run store);
no live DB (the candidate SQL itself is covered by the real-PG smoke).

Covers: the strict-AND candidates become proposals with the canonical payload + signals; directional
duplicates canonicalize + dedup to one proposal; the re-file guard skips a decided pair; the
per-run cap bounds filing; the watermark reads the last successful run; and the pure
``default_survivor`` pick (degree, then older, then id).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.dedup.store import DedupCandidate, NodeMergeStat
from app.dedup.sweep import AGENT, DedupSweepService, default_survivor
from app.services.agent_runs import SUCCEEDED, AgentRun
from app.services.review_queue import KIND_DEDUP_PROPOSAL
from tests.fakes import FakeAgentRunStore, FakeDedupStore, FakeReviewQueue

NOW = datetime(2026, 7, 16, 3, 0, 0, tzinfo=UTC)


def _cand(a, b, *, cosine=0.95, ents=("e1",), titles=("Ana",), overlap=True, ta="A", tb="B"):
    return DedupCandidate(
        node_a=a,
        node_b=b,
        cosine=cosine,
        shared_entity_ids=list(ents),
        shared_entity_titles=list(titles),
        occurred_overlap=overlap,
        title_a=ta,
        title_b=tb,
    )


def _stat(node_id, *, degree=0, created=None, indexed=None):
    return NodeMergeStat(
        node_id=node_id, degree=degree, node_created_at=created, indexed_at=indexed
    )


def _service(store, review, runs, settings=None):
    return DedupSweepService(
        settings=settings or Settings(),
        dedup_store=store,
        review_queue=review,
        run_store=runs,
    )


# --- filing --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_becomes_a_dedup_proposal():
    store = FakeDedupStore(
        candidates=[_cand("n-a", "n-b", cosine=0.933, ents=["e1"], titles=["Ana"])],
        stats={"n-a": _stat("n-a", degree=5), "n-b": _stat("n-b", degree=1)},
    )
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    await _service(store, review, runs).run_scheduled()

    assert len(review.items) == 1
    item = review.items[0]
    assert item.kind == KIND_DEDUP_PROPOSAL
    assert item.source == AGENT
    # Canonical least→greatest ids in the payload.
    assert item.payload["node_a"] == "n-a" and item.payload["node_b"] == "n-b"
    assert item.payload["signals"] == {
        "cosine": 0.933,
        "shared_entity_ids": ["e1"],
        "shared_entity_titles": ["Ana"],
        "occurred_overlap": True,
    }
    # Higher-degree node is the default survivor.
    assert item.payload["default_survivor"] == "n-a"
    assert item.excerpt and "possible duplicate" in item.excerpt
    # The run closed succeeded with the outcome details.
    run = next(iter(runs.runs.values()))
    assert run.status == SUCCEEDED
    assert run.details == {"pairs_scanned": 1, "proposals_filed": 1, "already_filed": 0}


@pytest.mark.asyncio
async def test_directional_duplicates_canonicalize_to_one_proposal():
    # The same pair surfaces from both drivers (b>a in one, a<b in the other) — one proposal, ids
    # canonicalized least→greatest regardless of the directional order the SQL returned.
    store = FakeDedupStore(
        candidates=[_cand("z-node", "a-node"), _cand("a-node", "z-node", cosine=0.9)],
        stats={"a-node": _stat("a-node", degree=2), "z-node": _stat("z-node", degree=2)},
    )
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    await _service(store, review, runs).run_scheduled()

    assert len(review.items) == 1
    assert review.items[0].payload["node_a"] == "a-node"
    assert review.items[0].payload["node_b"] == "z-node"


@pytest.mark.asyncio
async def test_refile_guard_skips_already_proposed_pair():
    store = FakeDedupStore(
        candidates=[_cand("n-a", "n-b"), _cand("n-c", "n-d")],
        existing={("n-a", "n-b")},  # canonical pair already has a dedup-proposal (any status)
        stats={},
    )
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    await _service(store, review, runs).run_scheduled()

    # Only the un-decided pair is filed; the decided one is counted, not re-proposed.
    filed_pairs = {(i.payload["node_a"], i.payload["node_b"]) for i in review.items}
    assert filed_pairs == {("n-c", "n-d")}
    run = next(iter(runs.runs.values()))
    assert run.details == {"pairs_scanned": 2, "proposals_filed": 1, "already_filed": 1}


@pytest.mark.asyncio
async def test_per_run_cap_bounds_filing():
    store = FakeDedupStore(
        candidates=[_cand("a1", "b1"), _cand("a2", "b2"), _cand("a3", "b3")],
        stats={},
    )
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    await _service(store, review, runs, Settings(dedup_max_pairs_per_run=2)).run_scheduled()

    assert len(review.items) == 2


@pytest.mark.asyncio
async def test_empty_scan_files_nothing_and_succeeds():
    store = FakeDedupStore(candidates=[], stats={})
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    await _service(store, review, runs).run_scheduled()

    assert review.items == []
    assert next(iter(runs.runs.values())).status == SUCCEEDED


@pytest.mark.asyncio
async def test_content_types_exclude_entity_like_types():
    # content = node_types minus entity_like_types (ADR-049 §3): both reach the candidate SQL.
    store = FakeDedupStore(candidates=[], stats={})
    review, runs = FakeReviewQueue(), FakeAgentRunStore()
    settings = Settings()

    await _service(store, review, runs, settings).run_scheduled()

    args = store.candidate_args
    assert set(args["entity_like_types"]) == set(settings.entity_like_types)
    assert "person" not in args["content_types"] and "memory" in args["content_types"]
    assert args["min_cosine"] == settings.dedup_min_cosine
    assert args["candidate_k"] == settings.dedup_candidate_k


# --- watermark -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watermark_reads_last_successful_run():
    last = NOW - timedelta(hours=6)
    runs = FakeAgentRunStore()
    runs.preloaded[AGENT] = AgentRun(id="prev", agent=AGENT, status=SUCCEEDED, started_at=last)
    store = FakeDedupStore(candidates=[], stats={})

    await _service(store, FakeReviewQueue(), runs).run_scheduled()

    # The candidate scan is bounded by the last successful run's start (not the window fallback).
    assert store.candidate_args["watermark"] == last


@pytest.mark.asyncio
async def test_watermark_falls_back_to_window_on_first_run():
    store = FakeDedupStore(candidates=[], stats={})
    runs = FakeAgentRunStore()  # no prior successful run

    await _service(store, FakeReviewQueue(), runs, Settings(dedup_window_days=1)).run_scheduled()

    watermark = store.candidate_args["watermark"]
    # Roughly now − window_days (a fresh store sweeps only recent captures, not all history).
    assert (datetime.now(UTC) - watermark) < timedelta(days=1, hours=1)
    assert (datetime.now(UTC) - watermark) > timedelta(hours=23)


# --- default survivor (pure, ADR-049 §6) ---------------------------------------------------


def test_default_survivor_prefers_higher_degree():
    stats = {"a": _stat("a", degree=1), "b": _stat("b", degree=7)}
    assert default_survivor("a", "b", stats) == "b"


def test_default_survivor_tiebreaks_on_older_created():
    older = datetime(2020, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 1, 1, tzinfo=UTC)
    stats = {
        "a": _stat("a", degree=3, created=newer),
        "b": _stat("b", degree=3, created=older),
    }
    assert default_survivor("a", "b", stats) == "b"  # keep the older original


def test_default_survivor_dated_beats_undated_on_age_tie():
    # Equal degree, one dated one undated: the dated (older-known) node wins the "older" tiebreak.
    dated = datetime(2021, 5, 1, tzinfo=UTC)
    stats = {"a": _stat("a", degree=0), "b": _stat("b", degree=0, created=dated)}
    assert default_survivor("a", "b", stats) == "b"


def test_default_survivor_falls_back_to_indexed_at():
    older = datetime(2022, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 1, 1, tzinfo=UTC)
    stats = {"a": _stat("a", degree=2, indexed=older), "b": _stat("b", degree=2, indexed=newer)}
    assert default_survivor("a", "b", stats) == "a"


def test_default_survivor_deterministic_id_tiebreak():
    # Everything equal (degree + no dates) → the least id wins, stable across runs.
    stats = {"zeta": _stat("zeta", degree=1), "alpha": _stat("alpha", degree=1)}
    assert default_survivor("zeta", "alpha", stats) == "alpha"
