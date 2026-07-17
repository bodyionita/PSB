"""JobRunner single-flight + manual-trigger tests (M8 task 1, ADR-053 §7)."""

from __future__ import annotations

import asyncio

import pytest

from app.services.agent_runs import MANUAL, SCHEDULED, current_trigger
from app.services.job_runner import JobAlreadyRunning, JobRunner


async def test_run_manual_sets_manual_trigger_scope():
    runner = JobRunner()
    seen: list[str] = []

    async def job() -> str:
        seen.append(current_trigger())  # the run opened here would be stamped `manual`
        return "done"

    assert await runner.run_manual("reindex", job) == "done"
    assert seen == [MANUAL]
    # The scope is unwound after the call — a later run defaults back to scheduled.
    assert current_trigger() == SCHEDULED
    assert not runner.is_running("reindex")  # slot released


async def test_run_manual_conflict_raises_and_leaves_holder_untouched():
    runner = JobRunner()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(runner.run_manual("reindex", slow))
    await started.wait()
    assert runner.is_running("reindex")

    with pytest.raises(JobAlreadyRunning):
        await runner.run_manual("reindex", slow)  # single-flight: the second trigger 409s

    release.set()
    await task
    assert not runner.is_running("reindex")  # the original holder still released cleanly


async def test_scheduled_step_yields_true_then_releases():
    runner = JobRunner()
    async with runner.scheduled_step("dedup-sweep") as acquired:
        assert acquired is True
        assert runner.is_running("dedup-sweep")
        assert "dedup-sweep" in runner.running_agents()
    assert not runner.is_running("dedup-sweep")


async def test_scheduled_step_skips_when_a_manual_run_holds_the_slot():
    runner = JobRunner()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(runner.run_manual("reindex", slow))
    await started.wait()

    async with runner.scheduled_step("reindex") as acquired:
        assert acquired is False  # a scheduled step is skipped, not blocked, on a manual collision

    release.set()
    await task


async def test_manual_run_409s_while_a_scheduled_step_holds_the_slot():
    runner = JobRunner()
    async with runner.scheduled_step("reindex") as acquired:
        assert acquired is True
        with pytest.raises(JobAlreadyRunning):
            await runner.run_manual("reindex", lambda: asyncio.sleep(0))


async def test_run_manual_releases_on_job_error():
    runner = JobRunner()

    async def boom() -> None:
        raise RuntimeError("job failed")

    with pytest.raises(RuntimeError):
        await runner.run_manual("reindex", boom)
    assert not runner.is_running("reindex")  # the slot is freed even when the job raises
    assert current_trigger() == SCHEDULED  # trigger scope unwound
