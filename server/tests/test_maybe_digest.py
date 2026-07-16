"""Maybe-digest tests (M6 task 8, ADR-048 §8) — the weekly job that emits one feed-visible
``agent_run`` summarizing the parked ``maybe`` review items. Exercised against fakes (a maybe-stats
store + run store); the aggregate SQL itself is covered by the real-PG smoke.

Covers: parked maybes become a run with the total + per-kind breakdown + oldest-age; the empty case
is still a (heartbeat) run, not a gap; the age is floored at 0 and tolerant of naive stamps; and a
store error ends the run ``failed`` (never raises — rule 7).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.services.agent_runs import FAILED, SUCCEEDED
from app.services.maybe_digest import AGENT, MaybeDigestService
from app.services.review_queue import MaybeKindStat
from tests.fakes import FakeAgentRunStore


class _FakeMaybeStore:
    """Returns a fixed maybe aggregate; ``boom`` makes the read raise (the rule-7 path)."""

    def __init__(self, stats: list[MaybeKindStat], *, boom: bool = False) -> None:
        self._stats = stats
        self._boom = boom

    async def maybe_kind_stats(self) -> list[MaybeKindStat]:
        if self._boom:
            raise RuntimeError("db down")
        return self._stats


def _service(store, runs) -> MaybeDigestService:
    return MaybeDigestService(settings=Settings(), store=store, run_store=runs)


def _last_run(runs: FakeAgentRunStore):
    assert len(runs.runs) == 1
    return next(iter(runs.runs.values()))


@pytest.mark.asyncio
async def test_parked_maybes_summarized_into_one_run():
    now = datetime.now(UTC)
    store = _FakeMaybeStore(
        [
            MaybeKindStat("stance-candidate", 3, now - timedelta(days=12)),
            MaybeKindStat("dedup-proposal", 1, now - timedelta(days=2)),
        ]
    )
    runs = FakeAgentRunStore()

    await _service(store, runs).run_scheduled()

    run = _last_run(runs)
    assert run.agent == AGENT
    assert run.status == SUCCEEDED
    assert run.details["total"] == 4
    assert run.details["by_kind"] == {"stance-candidate": 3, "dedup-proposal": 1}
    # oldest across kinds → the 12-day-old stance-candidate; age floored to whole days.
    assert run.details["oldest_age_days"] == 12
    assert "4 parked maybe(s)" in run.summary
    assert "oldest 12d old" in run.summary


@pytest.mark.asyncio
async def test_empty_digest_is_still_a_run():
    runs = FakeAgentRunStore()

    await _service(_FakeMaybeStore([]), runs).run_scheduled()

    run = _last_run(runs)
    assert run.status == SUCCEEDED
    assert run.details == {
        "total": 0,
        "by_kind": {},
        "oldest_created_at": None,
        "oldest_age_days": None,
    }
    assert run.summary == "maybe digest: no parked maybes"


@pytest.mark.asyncio
async def test_age_is_floored_and_tolerates_naive_stamps():
    # A future-dated (clock-skew) naive stamp must not produce a negative age, and a naive time is
    # read as UTC rather than raising on an aware/naive subtraction.
    naive_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    runs = FakeAgentRunStore()
    store = _FakeMaybeStore([MaybeKindStat("stance-candidate", 1, naive_future)])

    await _service(store, runs).run_scheduled()

    run = _last_run(runs)
    assert run.status == SUCCEEDED
    assert run.details["oldest_age_days"] == 0  # floored, no negative


@pytest.mark.asyncio
async def test_store_error_ends_the_run_failed_and_never_raises():
    runs = FakeAgentRunStore()

    await _service(_FakeMaybeStore([], boom=True), runs).run_scheduled()  # must not raise

    run = _last_run(runs)
    assert run.status == FAILED
    assert "maybe digest failed" in (run.summary or "")
