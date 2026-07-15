"""In-process job scheduler (ADR-010, durability Slice B2).

Wraps APScheduler's :class:`AsyncIOScheduler` and registers the M1 durability jobs — plus, from
M2, the combined nightly ``reindex`` job (ADR-023 §4) — on their crontab windows (all evaluated
in ``scheduler_tz``). Off unless ``enable_scheduler`` is set — exactly one prod instance runs it
(03:40 reindex + 04:55 sweep + nightly bundle/pg_dump/data-sync + weekly integrity drill). M4
extends the same scheduler with the rest of the 03:00–05:00 agent window.

The job → schedule mapping is exposed as pure data (:meth:`BackupScheduler.job_specs`) so it
unit-tests without a running event loop; ``start``/``shutdown`` are the only parts that touch
APScheduler. Each job already wraps itself in an ``agent_runs`` row and never raises (rule 7);
the store-touching jobs serialise on ``StoreBackupService``'s single lock, so overlapping fire
times are safe.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import Settings
from .backup_jobs import BackupJobs
from .reindex import ReindexService

logger = logging.getLogger(__name__)


class EntityJob(Protocol):
    """A nightly agent-window job (profile-refresh / backfill / identity-capsule) — one idempotent,
    never-raising entry point the scheduler and CLI both drive (ADR-030 §4/§6, ADR-046 §5). Some
    return an outcome for CLI logging; the scheduler ignores it."""

    async def run_scheduled(self): ...


@dataclass(frozen=True)
class JobSpec:
    """One scheduled job: a stable id, the coroutine to run, and its crontab expression."""

    id: str
    func: Callable[[], Awaitable[None]]
    crontab: str


class BackupScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        jobs: BackupJobs,
        reindex: ReindexService | None = None,
        profile_refresh: EntityJob | None = None,
        backfill: EntityJob | None = None,
        identity_capsule: EntityJob | None = None,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._reindex = reindex
        self._profile_refresh = profile_refresh
        self._backfill = backfill
        self._identity_capsule = identity_capsule
        self._tz = ZoneInfo(settings.scheduler_tz)
        self._scheduler = scheduler or AsyncIOScheduler(timezone=self._tz)

    def job_specs(self) -> list[JobSpec]:
        """The scheduled jobs and their schedules — pure, so it tests without a loop."""
        s = self._settings
        specs = [
            JobSpec("data-sync", self._jobs.run_data_sync, s.backup_data_sync_cron),
            JobSpec("db-backup", self._jobs.run_db_backup, s.backup_db_backup_cron),
            JobSpec("integrity-drill", self._jobs.run_integrity_drill, s.integrity_drill_cron),
            JobSpec("store-sweep", self._jobs.run_store_sweep, s.backup_store_sweep_cron),
            JobSpec("store-backup", self._jobs.run_store_bundle, s.backup_store_bundle_cron),
        ]
        # M2 (ADR-023 §4): the combined nightly reindex. Single-flight guards it against the
        # manual POST /admin/reindex; its own git work serialises on the store lock like the rest.
        if self._reindex is not None:
            specs.append(JobSpec("reindex", self._reindex.run_scheduled, s.reindex_cron))
        # M3 (ADR-030 §4/§6): the nightly entity jobs. Profile-refresh runs after the reindex (its
        # neighborhood reads want the day's edges in the DB); backfill after that. Both serialise
        # their store git work on the one lock, so overlapping fire times stay safe.
        if self._profile_refresh is not None:
            specs.append(
                JobSpec(
                    "profile-refresh", self._profile_refresh.run_scheduled, s.profile_refresh_cron
                )
            )
        if self._backfill is not None:
            specs.append(JobSpec("entity-backfill", self._backfill.run_scheduled, s.backfill_cron))
        # M5 (ADR-046 §5): the identity-capsule refresh. Runs after profile-refresh + backfill so
        # it distils over the day's fresh hubs; DB-only (app_settings), so it doesn't touch the
        # store lock. Never generated on a read — this nightly (or the on-demand trigger) is it.
        if self._identity_capsule is not None:
            specs.append(
                JobSpec(
                    "identity-capsule-refresh",
                    self._identity_capsule.run_scheduled,
                    s.identity_capsule_refresh_cron,
                )
            )
        return specs

    def start(self) -> None:
        """Register every job and start firing (must run inside the app's event loop)."""
        grace = self._settings.scheduler_misfire_grace_seconds
        for spec in self.job_specs():
            self._scheduler.add_job(
                spec.func,
                trigger=CronTrigger.from_crontab(spec.crontab, timezone=self._tz),
                id=spec.id,
                name=spec.id,
                # A missed fire is skipped (next night covers it, ADR-010); a backlog coalesces
                # to one run; a job never overlaps itself on the 4GB VPS.
                misfire_grace_time=grace,
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
        self._scheduler.start()
        logger.info(
            "scheduler started (%s): %s",
            self._settings.scheduler_tz,
            ", ".join(f"{s.id}={s.crontab}" for s in self.job_specs()),
        )

    def shutdown(self) -> None:
        """Stop firing new jobs. Non-blocking: an in-flight job is idempotent and re-runs."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler stopped")
