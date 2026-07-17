"""Graph-health reporter tests (M8 task 4, ADR-053 §9) — the nightly-tail read-only checker that
writes its seven findings into its own ``agent_runs.details`` (the console card reads the latest
run). Exercised against a fake store + run store; the check SQL itself is covered by the real-PG
smoke.

Covers: the seven checks fold into one succeeded run with counts + bounded samples; a clean graph is
still a (heartbeat) run; review-aging derives the oldest-age + threshold; a store error ends the run
``failed`` without raising (rule 7); and the pure freshness parse (newest ``(as of …)`` stamp +
staleness selection) in isolation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import Settings
from app.services.agent_runs import FAILED, SUCCEEDED
from app.services.graph_health import (
    AGENT,
    CHECK_ALIAS_LESS,
    CHECK_INBOX_DEPTH,
    CHECK_MISSING_OCCURRED,
    CHECK_ORPHAN_NODES,
    CHECK_REVIEW_AGING,
    CHECK_STALE_OBSERVATIONS,
    CHECK_TOMBSTONE_INTEGRITY,
    CountSample,
    GraphHealthService,
    Offender,
    ProfileObservations,
    ReviewAgingRaw,
    _count_sample,
    newest_as_of,
    stale_entity_profiles,
)
from tests.fakes import FakeAgentRunStore


class _FakeGraphHealthStore:
    """Returns canned per-check results; ``boom`` makes the first check raise (the rule-7 path)."""

    def __init__(
        self,
        *,
        orphans: CountSample | None = None,
        inbox: CountSample | None = None,
        aging: ReviewAgingRaw | None = None,
        missing: CountSample | None = None,
        alias_less: CountSample | None = None,
        tombstones: CountSample | None = None,
        profiles: list[ProfileObservations] | None = None,
        boom: bool = False,
    ) -> None:
        self._orphans = orphans or CountSample(0)
        self._inbox = inbox or CountSample(0)
        self._aging = aging or ReviewAgingRaw(decidable=0, aged=0, oldest_created_at=None)
        self._missing = missing or CountSample(0)
        self._alias_less = alias_less or CountSample(0)
        self._tombstones = tombstones or CountSample(0)
        self._profiles = profiles or []
        self._boom = boom

    async def orphan_nodes(self, *, inbox_prefix, sample):
        if self._boom:
            raise RuntimeError("db down")
        return self._orphans

    async def inbox_depth(self, *, inbox_prefix, sample):
        return self._inbox

    async def pending_review_aging(self, *, decidable, cutoff, sample):
        return self._aging

    async def memories_missing_occurred(self, *, sample):
        return self._missing

    async def alias_less_entities(self, *, entity_types, sample):
        return self._alias_less

    async def dangling_tombstones(self, *, sample):
        return self._tombstones

    async def entity_profiles(self, *, entity_types):
        return self._profiles


def _service(store, runs, settings: Settings | None = None) -> GraphHealthService:
    return GraphHealthService(settings=settings or Settings(), store=store, run_store=runs)


def _last_run(runs: FakeAgentRunStore):
    assert len(runs.runs) == 1
    return next(iter(runs.runs.values()))


def _checks_by_name(run) -> dict[str, dict]:
    return {c["check"]: c for c in run.details["checks"]}


@pytest.mark.asyncio
async def test_seven_checks_fold_into_one_run():
    now = datetime.now(UTC)
    store = _FakeGraphHealthStore(
        orphans=CountSample(2, [Offender("n1", "Orphan One"), Offender("n2", "Orphan Two")]),
        inbox=CountSample(3, [Offender("i1", "inbox/x.md")]),
        aging=ReviewAgingRaw(
            decidable=5,
            aged=2,
            oldest_created_at=now - timedelta(days=30),
            offenders=[Offender("r1", "entity-ambiguity (2026-06-01)")],
        ),
        missing=CountSample(1, [Offender("m1", "Undated memory")]),
        alias_less=CountSample(4),
        tombstones=CountSample(0),
        profiles=[
            ProfileObservations("e1", "Stale Person", [{"since": "2020-01-01"}]),
            ProfileObservations("e2", "Fresh Person", [{"since": now.date().isoformat()}]),
        ],
    )
    runs = FakeAgentRunStore()

    outcome = await _service(store, runs).run_scheduled()

    run = _last_run(runs)
    assert run.agent == AGENT
    assert run.status == SUCCEEDED
    checks = _checks_by_name(run)
    assert set(checks) == {
        CHECK_ORPHAN_NODES,
        CHECK_INBOX_DEPTH,
        CHECK_REVIEW_AGING,
        CHECK_MISSING_OCCURRED,
        CHECK_ALIAS_LESS,
        CHECK_TOMBSTONE_INTEGRITY,
        CHECK_STALE_OBSERVATIONS,
    }
    assert checks[CHECK_ORPHAN_NODES]["count"] == 2
    assert checks[CHECK_ORPHAN_NODES]["sample"][0] == {"id": "n1", "label": "Orphan One"}
    assert checks[CHECK_INBOX_DEPTH]["count"] == 3
    assert checks[CHECK_ALIAS_LESS]["count"] == 4
    assert checks[CHECK_TOMBSTONE_INTEGRITY]["count"] == 0
    assert checks[CHECK_STALE_OBSERVATIONS]["count"] == 1  # only the 2020 profile is stale
    # review-aging derives the oldest age + surfaces the configured threshold.
    aging = checks[CHECK_REVIEW_AGING]
    assert aging["count"] == 2
    assert aging["decidable"] == 5
    assert aging["aging_threshold_days"] == Settings().graph_health_review_aging_days
    assert aging["oldest_age_days"] == 30
    # 6 of 7 checks flagged (tombstones clean).
    assert run.details["flagged_checks"] == 6
    assert outcome is not None and outcome.flagged_checks == 6
    assert "6/7 check(s) flagged" in run.summary


@pytest.mark.asyncio
async def test_clean_graph_is_still_a_heartbeat_run():
    runs = FakeAgentRunStore()

    await _service(_FakeGraphHealthStore(), runs).run_scheduled()

    run = _last_run(runs)
    assert run.status == SUCCEEDED
    assert run.details["flagged_checks"] == 0
    assert len(run.details["checks"]) == 7
    assert all(c["count"] == 0 for c in run.details["checks"])
    assert "0/7 check(s) flagged" in run.summary


@pytest.mark.asyncio
async def test_store_error_ends_the_run_failed_and_never_raises():
    runs = FakeAgentRunStore()

    await _service(_FakeGraphHealthStore(boom=True), runs).run_scheduled()  # must not raise

    run = _last_run(runs)
    assert run.status == FAILED
    assert "graph-health failed" in (run.summary or "")


@pytest.mark.asyncio
async def test_freshness_threshold_is_config_driven():
    # With a 10-day window, a 30-day-old stamp is stale; with a 90-day window it is fresh.
    now = datetime.now(UTC)
    stamp = (now - timedelta(days=30)).date().isoformat()
    profiles = [ProfileObservations("e1", "Person", [{"since": stamp}])]

    tight = Settings(graph_health_freshness_days=10)
    loose = Settings(graph_health_freshness_days=90)

    runs_tight = FakeAgentRunStore()
    await _service(_FakeGraphHealthStore(profiles=profiles), runs_tight, tight).run_scheduled()
    assert _checks_by_name(_last_run(runs_tight))[CHECK_STALE_OBSERVATIONS]["count"] == 1

    runs_loose = FakeAgentRunStore()
    await _service(_FakeGraphHealthStore(profiles=profiles), runs_loose, loose).run_scheduled()
    assert _checks_by_name(_last_run(runs_loose))[CHECK_STALE_OBSERVATIONS]["count"] == 0


# --- count/sample decoupling (rule 7: LIMIT 0 must never zero a count) ---------------------------


def test_count_sample_preserves_count_when_sample_is_zero():
    # Mirrors the store's `count(*) … LEFT JOIN sample` row shape when a LIMIT-0 sample returns no
    # rows: one row carrying the true total with NULL sample columns. The count MUST survive (the
    # documented `graph_health_sample_offenders=0` "counts only" mode) and no phantom offender is
    # emitted for the NULL id.
    rows = [{"total": 50, "id": None, "title": None, "store_path": None}]
    result = _count_sample(rows, label_key="title", fallback_key="store_path")
    assert result.count == 50
    assert result.offenders == []


def test_count_sample_is_independent_of_sample_size():
    # Same match set (total=3); the count is read from `total`, not from the number of sampled rows,
    # so it is identical whether the sample is full or empty — offenders are bounded by the sample.
    full = [
        {"total": 3, "id": "a", "title": "A", "store_path": "p/a"},
        {"total": 3, "id": "b", "title": "B", "store_path": "p/b"},
    ]
    empty = [{"total": 3, "id": None, "title": None, "store_path": None}]

    full_result = _count_sample(full, label_key="title", fallback_key="store_path")
    empty_result = _count_sample(empty, label_key="title", fallback_key="store_path")
    assert full_result.count == empty_result.count == 3
    assert len(full_result.offenders) == 2
    assert empty_result.offenders == []


# --- pure freshness logic -----------------------------------------------------------------------


def test_newest_as_of_picks_the_latest_stamp():
    obs = [
        {"since": "2024-01-01"},
        {"since": "2025-06-15"},
        {"since": "2023-12-31"},
    ]
    assert newest_as_of(obs) == date(2025, 6, 15)


def test_newest_as_of_ignores_missing_and_malformed_stamps():
    obs = [{"since": None}, {"title": "no since key"}, {"since": "not-a-date"}, {"since": ""}]
    assert newest_as_of(obs) is None
    # a single valid stamp among junk is still found.
    assert newest_as_of([*obs, {"since": "2025-02-02"}]) == date(2025, 2, 2)


def test_stale_entity_profiles_selects_orders_and_bounds():
    cutoff = date(2025, 1, 1)
    profiles = [
        ProfileObservations("old1", "Old One", [{"since": "2020-05-05"}]),
        ProfileObservations("old2", "Old Two", [{"since": "2022-05-05"}]),
        ProfileObservations("fresh", "Fresh", [{"since": "2025-06-01"}]),
        ProfileObservations("undated", "Undated", [{"since": None}]),
    ]
    result = stale_entity_profiles(profiles, cutoff=cutoff, sample=1)
    assert result.count == 2  # old1 + old2; fresh + undated excluded
    # oldest-stamp-first, bounded to the sample size.
    assert len(result.offenders) == 1
    assert result.offenders[0].id == "old1"
    assert "as of 2020-05-05" in result.offenders[0].label
