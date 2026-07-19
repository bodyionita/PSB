"""Entity-hub dedup detector tests (M9.8 T4, ADR-064 §4) — the conservative same-type hub dedup
that surfaces high-confidence pairs inline (the run details) and files lower-confidence pairs to the
`entity-dedup` review queue. Exercised against fakes (hub store, review queue, run store); no live
DB (the hub-neighbourhood SQL itself is covered by the real-PG smoke).

Covers: the pure name gate (exact / containment / fuzzy + the low-entropy guard), the shared-
neighborhood gate, the high/low classification, the default-survivor pick; and the service — a
strong pair lands inline while a weak one files to review, **"Diana Wren" is suppressed** by the
shared-neighborhood AND leg, the re-file guard skips a tracked pair, and the per-run cap bounds it.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.entities.entity_dedup import (
    AGENT,
    EntityDedupService,
    HubRow,
    NameMatch,
    default_survivor,
    is_high_confidence,
    name_match,
    shared_overlap,
)
from app.services.agent_runs import SUCCEEDED
from app.services.review_queue import KIND_ENTITY_DEDUP
from tests.fakes import FakeAgentRunStore, FakeEntityDedupStore, FakeReviewQueue


def _hub(node_id, title, aliases, neighbors, *, node_type="person") -> HubRow:
    return HubRow(
        id=node_id,
        type=node_type,
        title=title,
        aliases=list(aliases),
        neighbor_ids=frozenset(neighbors),
    )


def _service(store, review, runs, settings=None):
    return EntityDedupService(
        settings=settings or Settings(),
        store=store,
        review_queue=review,
        run_store=runs,
    )


# --- pure name gate -------------------------------------------------------------------------


def test_name_match_exact():
    m = name_match(["diana"], ["diana"], min_token_len=4, fuzzy_min=0.82)
    assert m == NameMatch(kind="exact", score=1.0)


def test_name_match_containment():
    # "diana" ⊆ "diana vance" — a containment hit anchored by the significant "diana" token.
    m = name_match(["diana"], ["diana vance"], min_token_len=4, fuzzy_min=0.82)
    assert m is not None and m.kind == "containment"


def test_name_match_low_entropy_token_does_not_anchor():
    # Only the short token "ana" (len 3 < 4) overlaps → not a real name match, and the whole-string
    # fuzzy ("ana" vs "ana lee") is below the floor.
    assert name_match(["ana"], ["ana lee"], min_token_len=4, fuzzy_min=0.82) is None


def test_name_match_fuzzy_hit_and_miss():
    hit = name_match(["madalina"], ["madaline"], min_token_len=4, fuzzy_min=0.82)
    assert hit is not None and hit.kind == "fuzzy"
    # Two genuinely different names with no containment fall below the fuzzy floor.
    assert name_match(["diana vance"], ["diana wren"], min_token_len=4, fuzzy_min=0.82) is None


# --- pure shared-neighborhood + classification ----------------------------------------------


def test_shared_overlap_counts_common_neighbours():
    a = _hub("a", "Diana", ["Diana"], {"m1", "m2", "m3"})
    b = _hub("b", "Diana Vance", ["Diana Vance"], {"m1", "m2", "x"})
    count, jaccard = shared_overlap(a, b)
    assert count == 2
    assert jaccard == pytest.approx(2 / 4)


def test_is_high_confidence_rules():
    contain = NameMatch(kind="containment", score=0.5)
    assert is_high_confidence(contain, 2, high_min_shared=2, fuzzy_high=0.92)
    assert not is_high_confidence(contain, 1, high_min_shared=2, fuzzy_high=0.92)  # thin overlap
    weak_fuzzy = NameMatch(kind="fuzzy", score=0.85)
    assert not is_high_confidence(weak_fuzzy, 3, high_min_shared=2, fuzzy_high=0.92)
    strong_fuzzy = NameMatch(kind="fuzzy", score=0.95)
    assert is_high_confidence(strong_fuzzy, 2, high_min_shared=2, fuzzy_high=0.92)


def test_default_survivor_higher_degree_wins():
    big = _hub("big", "Diana", ["Diana"], {"m1", "m2", "m3", "m4"})
    small = _hub("small", "Diana Vance", ["Diana Vance"], {"m1"})
    assert default_survivor(big, small) == ("big", "small")
    assert default_survivor(small, big) == ("big", "small")


# --- service --------------------------------------------------------------------------------


async def test_high_confidence_pair_lands_inline():
    store = FakeEntityDedupStore(
        hubs=[
            _hub("d", "Diana", ["Diana"], {"m1", "m2", "m3", "p1"}),
            _hub("dv", "Diana Vance", ["Diana Vance"], {"m1", "m2", "p1"}),
        ]
    )
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    outcome = await _service(store, review, runs).run_scheduled()

    # No review item filed — the strong pair is inline in the run details.
    assert review.items == []
    assert len(outcome.high_confidence) == 1
    entry = outcome.high_confidence[0]
    assert entry["survivor"]["id"] == "d" and entry["loser"]["id"] == "dv"  # higher degree survives
    assert entry["type"] == "person"
    assert entry["signals"]["shared_count"] == 3
    # The run finished SUCCEEDED with the inline feed on its details.
    run = await runs.latest(AGENT)
    assert run.status == SUCCEEDED
    assert run.details["high_confidence"] == outcome.high_confidence


async def test_diana_wren_is_suppressed():
    # "Diana Wren" shares the first name (the name gate alone would flag her) but wires into a
    # DIFFERENT neighbourhood → the shared-neighborhood AND leg fails → she is never proposed.
    store = FakeEntityDedupStore(
        hubs=[
            _hub("d", "Diana", ["Diana"], {"m1", "m2", "m3"}),
            _hub("dw", "Diana Wren", ["Diana Wren"], {"x1", "x2"}),
        ]
    )
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    outcome = await _service(store, review, runs).run_scheduled()

    assert outcome.pairs_scanned == 0
    assert outcome.high_confidence == [] and review.items == []


async def test_low_confidence_pair_files_review():
    # Containment name match but only one shared neighbour (< high_min_shared) → low-confidence.
    store = FakeEntityDedupStore(
        hubs=[
            _hub("b", "Nora", ["Nora"], {"m1", "m9"}),
            _hub("bs", "Nora Stone", ["Nora Stone"], {"m1", "z"}),
        ]
    )
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    outcome = await _service(store, review, runs).run_scheduled()

    assert outcome.high_confidence == []
    assert outcome.low_confidence_filed == 1
    [item] = review.items
    assert item.kind == KIND_ENTITY_DEDUP and item.source == AGENT
    node_a, node_b = ("b", "bs") if "b" < "bs" else ("bs", "b")
    assert item.payload["node_a"] == node_a and item.payload["node_b"] == node_b
    assert item.payload["default_survivor"] in ("b", "bs")
    assert item.payload["signals"]["shared_count"] == 1


async def test_refile_guard_skips_tracked_pair():
    node_a, node_b = ("b", "bs") if "b" < "bs" else ("bs", "b")
    store = FakeEntityDedupStore(
        hubs=[
            _hub("b", "Nora", ["Nora"], {"m1", "m9"}),
            _hub("bs", "Nora Stone", ["Nora Stone"], {"m1", "z"}),
        ],
        existing={(node_a, node_b)},
    )
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    outcome = await _service(store, review, runs).run_scheduled()

    assert review.items == []
    assert outcome.already_tracked == 1
    assert outcome.low_confidence_filed == 0 and outcome.high_confidence == []


async def test_per_run_cap_bounds_filing():
    hubs = []
    for i in range(4):
        # Each pair shares a distinct single neighbour → low-confidence containment pairs.
        hubs.append(_hub(f"n{i}", f"Sam{i}", [f"Sam{i}"], {f"s{i}"}))
        hubs.append(_hub(f"n{i}full", f"Sam{i} Fox", [f"Sam{i} Fox"], {f"s{i}"}))
    store = FakeEntityDedupStore(hubs=hubs)
    review, runs = FakeReviewQueue(), FakeAgentRunStore()
    settings = Settings(entity_dedup_max_pairs_per_run=2)

    outcome = await _service(store, review, runs, settings).run_scheduled()

    assert outcome.low_confidence_filed == 2  # capped
    assert len(review.items) == 2


async def test_types_reach_the_store():
    store = FakeEntityDedupStore(hubs=[])
    review, runs = FakeReviewQueue(), FakeAgentRunStore()

    await _service(store, review, runs).run_scheduled()

    assert store.hub_rows_arg == list(Settings().entity_like_types)
