"""RosterService tests (M8 task 3, ADR-053 §6/§7): the agents/pipelines projection over a fake
live scheduler + ``agent_runs`` store, and the manual triggers over a real JobRunner single-flight
guard. No live DB/scheduler (08 testing policy)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import roster as roster_module
from app.services.agent_runs import MANUAL, RUNNING, SUCCEEDED, AgentRun, current_trigger
from app.services.job_runner import JobAlreadyRunning, JobRunner
from app.services.pipeline import PipelineDef, PipelineStepDef
from app.services.roster import (
    AGENTS_JOBS,
    RosterService,
    SchedulerUnavailable,
    UnknownAgent,
    UnknownPipeline,
)

from .fakes import FakeAgentRunStore


def _def(name: str, cron: str, *steps: str) -> PipelineDef:
    return PipelineDef(name=name, cron=cron, steps=tuple(PipelineStepDef(name=s) for s in steps))


class _FakeAps:
    """Stands in for the wrapped APScheduler — ``get_job(name).next_run_time`` only."""

    def __init__(self, next_runs: dict[str, datetime]) -> None:
        self._next_runs = next_runs

    def get_job(self, job_id: str):
        dt = self._next_runs.get(job_id)
        return SimpleNamespace(next_run_time=dt) if dt is not None else None


class _FakeScheduler:
    """The slice of PipelineScheduler the roster reads: ``pipeline_runners()`` (public),
    ``_step_funcs()`` (the live runnable map), and ``_scheduler`` (for ``next_run_time``)."""

    def __init__(self, pairs, step_funcs, next_runs=None) -> None:
        self._pairs = pairs  # list[(PipelineDef, runner)]
        self._funcs = step_funcs  # dict[str, callable]
        self._scheduler = _FakeAps(next_runs or {})

    def pipeline_runners(self):
        return list(self._pairs)

    def _step_funcs(self):
        return dict(self._funcs)


class _RecordingRunner:
    """A pipeline runner whose ``run`` records that it fired + the trigger origin it saw."""

    def __init__(self, seen: list[str]) -> None:
        self._seen = seen
        self.ran = 0

    async def run(self):
        self.ran += 1
        self._seen.append(current_trigger())


def _roster(scheduler, store, job_runner=None, settings=None) -> RosterService:
    return RosterService(
        scheduler=scheduler,
        run_store=store,
        job_runner=job_runner or JobRunner(),
        settings=settings or Settings(),
    )


async def _drain_background() -> None:
    """Await the module-level backgrounded manual runs so a test can assert their effect."""
    for _ in range(50):
        tasks = list(roster_module._BACKGROUND_TASKS)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


# --- agents() ---------------------------------------------------------------------------------


async def test_agents_roster_order_membership_and_last_run():
    nightly = _def("nightly", "0 3 * * *", "reindex", "dedup-sweep")
    weekly = _def("weekly", "30 4 * * sun", "reindex", "maybe-digest")  # reindex in BOTH (0..N)
    funcs = {n: (lambda: None) for n in ("reindex", "dedup-sweep", "maybe-digest")}
    store = FakeAgentRunStore()
    store.preloaded["reindex"] = AgentRun(
        id="run-x", agent="reindex", status=SUCCEEDED, finished_at=datetime.now(UTC)
    )
    roster = _roster(_FakeScheduler([(nightly, None), (weekly, None)], funcs), store)

    entries = await roster.agents()
    # Nightly-pipeline order, first-appearance dedup across pipelines (ADR-053 §8).
    assert [e.name for e in entries] == ["reindex", "dedup-sweep", "maybe-digest"]
    reindex = entries[0]
    assert reindex.category == AGENTS_JOBS
    assert reindex.pipelines == ["nightly", "weekly"]  # many-to-many membership
    assert reindex.last_run is not None
    assert reindex.last_run.status == SUCCEEDED
    assert reindex.last_run.run_id == "run-x"
    # A never-run job → no last_run.
    assert entries[1].last_run is None
    assert entries[1].pipelines == ["nightly"]


async def test_agents_running_flag_reflects_job_runner():
    nightly = _def("nightly", "0 3 * * *", "reindex")
    funcs = {"reindex": (lambda: None)}
    job_runner = JobRunner()
    roster = _roster(_FakeScheduler([(nightly, None)], funcs), FakeAgentRunStore(), job_runner)

    async with job_runner.scheduled_step("reindex") as acquired:
        assert acquired
        entries = await roster.agents()
    assert entries[0].running is True
    # released after the scheduled_step scope
    assert (await roster.agents())[0].running is False


async def test_agents_include_unwired_step_funcs_appended():
    nightly = _def("nightly", "0 3 * * *", "reindex")
    # `graph-health` is in the live runnable map but not (yet) in a pipeline def → appended, []
    funcs = {"reindex": (lambda: None), "graph-health": (lambda: None)}
    roster = _roster(_FakeScheduler([(nightly, None)], funcs), FakeAgentRunStore())
    entries = await roster.agents()
    names = [e.name for e in entries]
    assert names == ["reindex", "graph-health"]
    assert entries[1].pipelines == []


# --- pipelines() ------------------------------------------------------------------------------


async def test_pipelines_projection_with_next_run():
    nightly = _def("nightly", "0 3 * * *", "reindex", "dedup-sweep")
    when = datetime(2026, 7, 18, 3, 0, tzinfo=UTC)
    scheduler = _FakeScheduler([(nightly, None)], {}, next_runs={"nightly": when})
    store = FakeAgentRunStore()
    store.preloaded["nightly"] = AgentRun(id="p1", agent="nightly", status=RUNNING)
    roster = _roster(scheduler, store)

    pipelines = await roster.pipelines()
    assert len(pipelines) == 1
    p = pipelines[0]
    assert p.name == "nightly"
    assert p.cron == "0 3 * * *"
    assert p.steps == ["reindex", "dedup-sweep"]
    assert p.next_run == when
    assert p.last_run is not None and p.last_run.run_id == "p1"


async def test_pipelines_fallback_to_config_when_scheduler_off():
    # Scheduler disabled on this instance → definitions from config, next_run null.
    roster = _roster(None, FakeAgentRunStore(), settings=Settings())
    pipelines = await roster.pipelines()
    names = {p.name for p in pipelines}
    assert {"nightly", "weekly"} <= names
    assert all(p.next_run is None for p in pipelines)


# --- trigger_agent ----------------------------------------------------------------------------


async def test_trigger_agent_backgrounds_under_manual_scope():
    seen: list[str] = []

    async def reindex_job():
        seen.append(current_trigger())  # the run this opens would be stamped `manual`

    scheduler = _FakeScheduler([], {"reindex": reindex_job})
    job_runner = JobRunner()
    roster = _roster(scheduler, FakeAgentRunStore(), job_runner)

    await roster.trigger_agent("reindex")
    await _drain_background()
    assert seen == [MANUAL]
    assert not job_runner.is_running("reindex")  # slot released after the run


async def test_trigger_agent_unknown_raises():
    roster = _roster(_FakeScheduler([], {"reindex": (lambda: None)}), FakeAgentRunStore())
    with pytest.raises(UnknownAgent):
        await roster.trigger_agent("nope")


async def test_trigger_agent_conflict_when_already_running():
    scheduler = _FakeScheduler([], {"reindex": (lambda: None)})
    job_runner = JobRunner()
    roster = _roster(scheduler, FakeAgentRunStore(), job_runner)
    async with job_runner.scheduled_step("reindex") as acquired:
        assert acquired
        with pytest.raises(JobAlreadyRunning):
            await roster.trigger_agent("reindex")


async def test_trigger_agent_scheduler_off_raises_unavailable():
    roster = _roster(None, FakeAgentRunStore())
    with pytest.raises(SchedulerUnavailable):
        await roster.trigger_agent("reindex")


# --- trigger_pipeline -------------------------------------------------------------------------


async def test_trigger_pipeline_backgrounds_under_manual_scope():
    seen: list[str] = []
    runner = _RecordingRunner(seen)
    nightly = _def("nightly", "0 3 * * *", "reindex")
    scheduler = _FakeScheduler([(nightly, runner)], {"reindex": (lambda: None)})
    job_runner = JobRunner()
    roster = _roster(scheduler, FakeAgentRunStore(), job_runner)

    await roster.trigger_pipeline("nightly")
    await _drain_background()
    assert runner.ran == 1
    assert seen == [MANUAL]
    assert not job_runner.is_running("nightly")


async def test_trigger_pipeline_unknown_raises():
    nightly = _def("nightly", "0 3 * * *", "reindex")
    scheduler = _FakeScheduler([(nightly, _RecordingRunner([]))], {})
    roster = _roster(scheduler, FakeAgentRunStore())
    with pytest.raises(UnknownPipeline):
        await roster.trigger_pipeline("weekly")


async def test_trigger_pipeline_conflict_when_already_running():
    runner = _RecordingRunner([])
    nightly = _def("nightly", "0 3 * * *", "reindex")
    job_runner = JobRunner()
    scheduler = _FakeScheduler([(nightly, runner)], {"reindex": (lambda: None)})
    roster = _roster(scheduler, FakeAgentRunStore(), job_runner)
    async with job_runner.scheduled_step("nightly") as acquired:
        assert acquired
        with pytest.raises(JobAlreadyRunning):
            await roster.trigger_pipeline("nightly")
