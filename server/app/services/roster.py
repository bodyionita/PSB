"""Agents + pipelines roster and manual triggers (M8 task 3, ADR-053 §6/§7).

The read side of the ops console's ops surface: a **flat roster** of individual jobs (each with its
0..N pipeline memberships + last-run) and **pipelines as a first-class resource** (cron, next-run,
ordered steps, last-run). Both are pure projection over data that already exists — the live
:class:`~app.services.scheduler.PipelineScheduler` (pipeline definitions + APScheduler
``next_run_time``) and the ``agent_runs`` store (last-run) — plus the in-process
:class:`~app.services.job_runner.JobRunner` for live running status (ADR-053 §6).

The write side (``POST /agents/{name}/run`` / ``POST /pipelines/{name}/run``) drives the **T1
JobRunner single-flight guard**: :meth:`~app.services.job_runner.JobRunner.run_manual` takes the
per-name slot (or raises :class:`~app.services.job_runner.JobAlreadyRunning` → ``409``) and wraps
the job in ``trigger_scope(MANUAL)`` so the run + its children file under *manual actions* (ADR-053
§5/§7). A trigger is **backgrounded** (``202`` then poll) — a nightly reindex or a whole pipeline
runs for minutes, so the request must not block; the ops console tails the run via ``GET /agents`` /
``GET /pipelines`` ``last_run.run_id`` → ``GET /activity/runs/{id}/logs`` (ADR-053 §11).

The scheduler is **authoritative single-flight** because it runs single-process (ADR-053 §7), so a
step already running as part of the live nightly pipeline (which marks itself via
``scheduled_step``) makes ``is_running`` true → a concurrent manual trigger of that agent ``409``s,
exactly as the contract requires ("409 if that agent — or a live pipeline it is a step of — is
running", 03-api §Activity & ops).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..config import Settings
from .agent_runs import AgentRun, AgentRunStore
from .job_runner import JobAlreadyRunning, JobRunner
from .pipeline import PipelineDef

if TYPE_CHECKING:
    from .scheduler import PipelineScheduler

logger = logging.getLogger(__name__)

# Feed origin-category (ADR-053 §4) reported for every roster job: they are all scheduled/background
# agent jobs. The finer feed split (conversations / manual-actions) is by run *origin* not by a
# job's identity, and ADR-053/03-api define no finer per-job taxonomy — so the roster reports this
# single category rather than inventing one.
AGENTS_JOBS = "agents_jobs"

# Strong references to in-flight backgrounded manual runs. asyncio keeps only a weak reference to a
# task, so without anchoring it here a run could be garbage-collected mid-flight — and the
# RosterService is built per-request (it can't own them). Discarded on completion.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


class UnknownAgent(LookupError):
    """No zero-arg job is registered under this name (→ ``404``)."""


class UnknownPipeline(LookupError):
    """No pipeline is defined under this name (→ ``404``)."""


class SchedulerUnavailable(RuntimeError):
    """The scheduler is not running on this instance, so nothing is runnable here (→ ``503``).

    Prod runs the scheduler in the single API process (ADR-003 single service + ADR-053 §7
    authoritative single-flight), so this only arises on a scheduler-disabled instance."""


@dataclass(frozen=True)
class LastRun:
    """The most recent ``agent_runs`` row for an agent/pipeline — the roster's ``last_run``."""

    status: str
    finished_at: datetime | None
    run_id: str


@dataclass(frozen=True)
class AgentEntry:
    """One roster row: a job, its 0..N pipeline memberships (many-to-many, ADR-053 §6), live
    running status, and its last run."""

    name: str
    category: str
    pipelines: list[str]
    running: bool
    last_run: LastRun | None


@dataclass(frozen=True)
class PipelineEntry:
    """One pipeline as a first-class resource: cadence, next-run (from the live scheduler), ordered
    steps, and its last run (ADR-053 §6)."""

    name: str
    cron: str
    next_run: datetime | None
    steps: list[str]
    last_run: LastRun | None


def _last_run(run: AgentRun | None) -> LastRun | None:
    if run is None:
        return None
    return LastRun(status=run.status, finished_at=run.finished_at, run_id=run.id)


class RosterService:
    """Projects the live scheduler + ``agent_runs`` into the agents/pipelines roster and drives the
    manual triggers over the JobRunner. Constructed per-request from ``app.state`` singletons — it
    holds no state of its own (the background-task anchor is module-level)."""

    def __init__(
        self,
        *,
        scheduler: PipelineScheduler | None,
        run_store: AgentRunStore,
        job_runner: JobRunner,
        settings: Settings,
    ) -> None:
        self._scheduler = scheduler
        self._runs = run_store
        self._job_runner = job_runner
        self._settings = settings

    # --- pipeline-definition source ------------------------------------------------------------
    def _pipeline_defs(self) -> list[PipelineDef]:
        """The live pipeline definitions. Sourced from the running scheduler (``pipeline_runners()``
        — already filtered to wired steps, ADR-047 §5) so the roster reflects exactly what runs; on
        a scheduler-disabled instance it falls back to the config definitions so the schedule is
        still inspectable (next-run is then null — nothing is scheduled here)."""
        if self._scheduler is not None:
            return [defn for defn, _ in self._scheduler.pipeline_runners()]
        return list(self._settings.pipeline_defs())

    def _membership(self, defs: list[PipelineDef]) -> dict[str, list[str]]:
        """Agent name → the pipelines it is a step of (0..N, many-to-many, ADR-053 §6)."""
        membership: dict[str, list[str]] = {}
        for defn in defs:
            for step in defn.steps:
                pipelines = membership.setdefault(step.name, [])
                if defn.name not in pipelines:
                    pipelines.append(defn.name)
        return membership

    def _ordered_agent_names(self, defs: list[PipelineDef]) -> list[str]:
        """Every runnable agent, in nightly-pipeline order (first appearance across the pipelines,
        preserving step order — ADR-053 §8 "listed in nightly-pipeline order"). When the scheduler
        is live its full step-func map is the authoritative runnable set (so an agent T4 adds — e.g.
        ``graph-health`` — appears automatically); any not already placed by pipeline order are
        appended."""
        ordered: list[str] = []
        seen: set[str] = set()
        for defn in defs:
            for step in defn.steps:
                if step.name not in seen:
                    seen.add(step.name)
                    ordered.append(step.name)
        if self._scheduler is not None:
            # The scheduler's step-func map is the authoritative live runnable set (ADR-053 §6);
            # there is no public accessor, so the roster reads it off the running scheduler.
            for name in self._scheduler._step_funcs():
                if name not in seen:
                    seen.add(name)
                    ordered.append(name)
        return ordered

    # --- reads ---------------------------------------------------------------------------------
    async def agents(self) -> list[AgentEntry]:
        """The flat roster — one entry per runnable job, in nightly-pipeline order (ADR-053 §6)."""
        defs = self._pipeline_defs()
        membership = self._membership(defs)
        running = self._job_runner.running_agents()
        entries: list[AgentEntry] = []
        for name in self._ordered_agent_names(defs):
            latest = await self._runs.latest(name)
            entries.append(
                AgentEntry(
                    name=name,
                    category=AGENTS_JOBS,
                    pipelines=membership.get(name, []),
                    running=name in running,
                    last_run=_last_run(latest),
                )
            )
        return entries

    async def pipelines(self) -> list[PipelineEntry]:
        """Pipelines as a first-class resource — cron, next-run (live scheduler), ordered steps,
        last-run (ADR-053 §6)."""
        entries: list[PipelineEntry] = []
        for defn in self._pipeline_defs():
            latest = await self._runs.latest(defn.name)
            entries.append(
                PipelineEntry(
                    name=defn.name,
                    cron=defn.cron,
                    next_run=self._next_run(defn.name),
                    steps=[step.name for step in defn.steps],
                    last_run=_last_run(latest),
                )
            )
        return entries

    def _next_run(self, pipeline_name: str) -> datetime | None:
        """The live APScheduler ``next_run_time`` for a pipeline's cron job (ADR-053 §6). Read from
        the running scheduler's APScheduler instance — there is no public accessor, so this reaches
        the wrapped scheduler defensively; null when the scheduler is off or the job isn't
        registered."""
        scheduler = self._scheduler
        if scheduler is None:
            return None
        aps = getattr(scheduler, "_scheduler", None)
        if aps is None:
            return None
        try:
            job = aps.get_job(pipeline_name)
        except Exception:  # noqa: BLE001 — a live-scheduler read must never fail the roster
            logger.debug("could not read next_run_time for %s", pipeline_name, exc_info=True)
            return None
        return getattr(job, "next_run_time", None) if job is not None else None

    # --- triggers ------------------------------------------------------------------------------
    async def trigger_agent(self, name: str) -> None:
        """Manually run one zero-arg job over the single-flight guard, stamped ``manual`` (ADR-053
        §6/§7). Raises :class:`SchedulerUnavailable` (503), :class:`UnknownAgent` (404), or
        :class:`JobAlreadyRunning` (409); otherwise backgrounds the run and returns."""
        func = self._runnable(name)
        self._claim_and_spawn(name, lambda: self._job_runner.run_manual(name, func))

    async def trigger_pipeline(self, name: str) -> None:
        """Manually run a whole pipeline over the single-flight guard, stamped ``manual`` (the
        ADR-047 §6 CLI verb over HTTP). The pipeline runner already marks each step via
        ``scheduled_step``, so its steps stay single-flighted against the live nightly run. Raises
        :class:`SchedulerUnavailable` (503), :class:`UnknownPipeline` (404), or
        :class:`JobAlreadyRunning` (409)."""
        runner = self._pipeline_runner(name)
        self._claim_and_spawn(name, lambda: self._job_runner.run_manual(name, runner.run))

    def _runnable(self, name: str):
        if self._scheduler is None:
            raise SchedulerUnavailable(name)
        # The live runnable map (ADR-053 §6) — no public accessor exists on the scheduler.
        func = self._scheduler._step_funcs().get(name)
        if func is None:
            raise UnknownAgent(name)
        return func

    def _pipeline_runner(self, name: str):
        if self._scheduler is None:
            raise SchedulerUnavailable(name)
        for defn, runner in self._scheduler.pipeline_runners():
            if defn.name == name:
                return runner
        raise UnknownPipeline(name)

    def _claim_and_spawn(self, name: str, job: Callable[[], Awaitable[object]]) -> None:
        """Synchronous single-flight pre-check (fast, authoritative ``409``) then background it.
        The pre-check and the background ``run_manual``'s own ``_acquire`` both key on ``name``; the
        run_manual acquire is authoritative, and the tiny same-tick race (two concurrent triggers of
        one agent) is closed by that acquire — the loser raises ``JobAlreadyRunning`` inside the
        backgrounded task and is logged (data-safe: every job is idempotent, rule 6)."""
        if self._job_runner.is_running(name):
            raise JobAlreadyRunning(name)
        self._spawn(job())

    def _spawn(self, coro: Awaitable[object]) -> None:
        async def _guard() -> None:
            try:
                await coro
            except JobAlreadyRunning:
                logger.info("manual trigger lost the single-flight race (already running)")
            except Exception:  # noqa: BLE001 — a job shouldn't raise (rule 7); surface, never crash
                logger.exception("backgrounded manual run failed")

        task = asyncio.create_task(_guard())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
