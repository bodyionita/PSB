"""In-process pipeline scheduler (ADR-047: the pipeline is the scheduling primitive).

Wraps APScheduler's :class:`AsyncIOScheduler` and registers **one cron per pipeline**, not one per
job (ADR-047 §7). Each pipeline (``nightly``/``weekly``, defined in
:meth:`~app.config.Settings.pipeline_defs`) runs its steps **sequentially on completion** from a
single start via a :class:`~app.services.pipeline.PipelineRunner`; the 03:00–05:00 window (ADR-010)
is now enforced by *sequencing from a 03:00 start*, not by the retired per-job stagger. Off unless
``enable_scheduler`` is set — exactly one prod instance runs it.

The pipeline → runner mapping is exposed as pure data (:meth:`PipelineScheduler.pipeline_runners`)
so it unit-tests without a running event loop; ``start``/``shutdown`` are the only parts that touch
APScheduler. A step's job already wraps itself in a child ``agent_runs`` row and never raises (rule
7); the runner opens the parent row and links them (ADR-047 §5). Store-touching steps serialise on
``StoreBackupService``'s single lock, and each pipeline cron is registered ``max_instances=1`` +
``coalesce`` so one long night can't overlap the next (the RAM guarantee).

Jobs stay independently invokable (CLI ``python -m app.cli <job>`` + ``POST /agents/{name}/run``,
invariant 4) — the pipeline owns the *schedule*, not the *only* way to run a step (ADR-047 §6).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import Settings
from .agent_runs import AgentRunStore
from .backup_jobs import BackupJobs
from .job_runner import JobRunner
from .pipeline import PipelineDef, PipelineRunner, StepFunc
from .reindex import ReindexService

logger = logging.getLogger(__name__)


class EntityJob(Protocol):
    """A nightly agent-window / sleep-cycle job (reindex / profile-refresh / backfill /
    identity-capsule / chat-distiller / inbox-drain / dedup-sweep / maybe-digest / graph-health /
    occurred-enrichment) —
    one idempotent, never-raising entry point the scheduler and CLI both drive (ADR-030 §4/§6,
    ADR-046 §5, ADR-048, ADR-053 §9).
    Some return an outcome for CLI logging; the pipeline runner ignores it."""

    async def run_scheduled(self) -> object | None: ...


class PipelineScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        jobs: BackupJobs,
        run_store: AgentRunStore,
        reindex: ReindexService | None = None,
        profile_refresh: EntityJob | None = None,
        backfill: EntityJob | None = None,
        identity_capsule: EntityJob | None = None,
        chat_distiller: EntityJob | None = None,
        inbox_drain: EntityJob | None = None,
        dedup_sweep: EntityJob | None = None,
        maybe_digest: EntityJob | None = None,
        graph_health: EntityJob | None = None,
        occurred_enrichment: EntityJob | None = None,
        scheduler: AsyncIOScheduler | None = None,
        job_runner: JobRunner | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._runs = run_store
        # The shared single-flight guard (M8, ADR-053 §7) threaded into every PipelineRunner so a
        # nightly step and a manual `POST /agents/{name}/run` of the same agent can't both run.
        self._job_runner = job_runner
        self._reindex = reindex
        self._profile_refresh = profile_refresh
        self._backfill = backfill
        self._identity_capsule = identity_capsule
        self._chat_distiller = chat_distiller
        self._inbox_drain = inbox_drain
        self._dedup_sweep = dedup_sweep
        self._maybe_digest = maybe_digest
        self._graph_health = graph_health
        self._occurred_enrichment = occurred_enrichment
        self._tz = ZoneInfo(settings.scheduler_tz)
        self._scheduler = scheduler or AsyncIOScheduler(timezone=self._tz)

    def _step_funcs(self) -> dict[str, StepFunc]:
        """Map each pipeline step name → the job coroutine that runs it. The five backup jobs are
        always wired; the agent-window + M6 sleep-cycle jobs are present only when their service was
        constructed (they stay optional, as before — prod wires them all). A step whose job is
        absent is dropped by :meth:`pipeline_runners` — never scheduled as a doomed ``missing`` step
        (ADR-047 §5)."""
        funcs: dict[str, StepFunc] = {
            "data-sync": self._jobs.run_data_sync,
            "db-backup": self._jobs.run_db_backup,
            "integrity-drill": self._jobs.run_integrity_drill,
            "store-sweep": self._jobs.run_store_sweep,
            "store-backup": self._jobs.run_store_bundle,
        }
        if self._reindex is not None:
            funcs["reindex"] = self._reindex.run_scheduled
        if self._profile_refresh is not None:
            funcs["profile-refresh"] = self._profile_refresh.run_scheduled
        if self._backfill is not None:
            funcs["entity-backfill"] = self._backfill.run_scheduled
        if self._identity_capsule is not None:
            funcs["identity-capsule-refresh"] = self._identity_capsule.run_scheduled
        # M6 sleep-cycle steps (ADR-048): woven into their dependency slots by `pipeline_defs`.
        if self._chat_distiller is not None:
            funcs["chat-distiller"] = self._chat_distiller.run_scheduled
        if self._inbox_drain is not None:
            funcs["inbox-drain"] = self._inbox_drain.run_scheduled
        if self._dedup_sweep is not None:
            funcs["dedup-sweep"] = self._dedup_sweep.run_scheduled
        if self._maybe_digest is not None:
            funcs["maybe-digest"] = self._maybe_digest.run_scheduled
        # M8.2 (ADR-056 §7): the undated-node flagger, a read-mostly tail step of `nightly`.
        if self._occurred_enrichment is not None:
            funcs["occurred-enrichment"] = self._occurred_enrichment.run_scheduled
        # M8 nightly-tail (ADR-053 §9): the read-only graph-health reporter, last step of `nightly`.
        if self._graph_health is not None:
            funcs["graph-health"] = self._graph_health.run_scheduled
        return funcs

    def pipeline_runners(self) -> list[tuple[PipelineDef, PipelineRunner]]:
        """One ``(definition, runner)`` per configured pipeline — pure, so it tests without a loop.

        Each pipeline's steps are filtered to those whose job is wired (:meth:`_step_funcs`); an
        unwired step is dropped **with a log** (rule 7: no silent caps) so it isn't recorded as a
        failed ``missing`` step every night. A pipeline left with no wired steps is skipped."""
        funcs = self._step_funcs()
        runners: list[tuple[PipelineDef, PipelineRunner]] = []
        for defn in self._settings.pipeline_defs():
            present = tuple(s for s in defn.steps if s.name in funcs)
            dropped = [s.name for s in defn.steps if s.name not in funcs]
            if dropped:
                logger.warning(
                    "pipeline %s: %d step(s) not wired, omitted from this run: %s",
                    defn.name,
                    len(dropped),
                    ", ".join(dropped),
                )
            if not present:
                logger.warning("pipeline %s: no wired steps, not scheduled", defn.name)
                continue
            effective = replace(defn, steps=present)
            runner = PipelineRunner(
                definition=effective,
                step_funcs=funcs,
                run_store=self._runs,
                job_runner=self._job_runner,
            )
            runners.append((effective, runner))
        return runners

    def start(self) -> None:
        """Register one cron per pipeline and start firing (runs inside the app's event loop)."""
        grace = self._settings.scheduler_misfire_grace_seconds
        registered = self.pipeline_runners()
        for defn, runner in registered:
            self._scheduler.add_job(
                runner.run,
                trigger=CronTrigger.from_crontab(defn.cron, timezone=self._tz),
                id=defn.name,
                name=defn.name,
                # A missed fire is skipped (next night covers it, ADR-010); a backlog coalesces to
                # one run; a pipeline never overlaps itself on the 4GB VPS — so one long night can't
                # stack on the next (the RAM guarantee the stagger used to provide, ADR-047 §3).
                misfire_grace_time=grace,
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
        self._scheduler.start()
        logger.info(
            "pipeline scheduler started (%s): %s",
            self._settings.scheduler_tz,
            ", ".join(
                f"{d.name}={d.cron} [{', '.join(s.name for s in d.steps)}]" for d, _ in registered
            ),
        )

    def shutdown(self) -> None:
        """Stop firing new pipelines. Non-blocking: an in-flight step is idempotent and re-runs."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("pipeline scheduler stopped")
