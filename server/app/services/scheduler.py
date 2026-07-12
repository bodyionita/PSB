"""In-process job scheduler (ADR-010, durability Slice B2).

Wraps APScheduler's :class:`AsyncIOScheduler` and registers the M1 durability jobs on their
crontab windows (all evaluated in ``scheduler_tz``). Off unless ``enable_scheduler`` is set —
exactly one prod instance runs it (04:55 sweep + nightly bundle/pg_dump/data-sync + weekly
integrity drill). M4 extends the same scheduler with the 03:00–05:00 agent window.

The job → schedule mapping is exposed as pure data (:meth:`BackupScheduler.job_specs`) so it
unit-tests without a running event loop; ``start``/``shutdown`` are the only parts that touch
APScheduler. Each job already wraps itself in an ``agent_runs`` row and never raises (rule 7);
the vault-touching jobs serialise on ``VaultBackupService``'s single lock, so overlapping fire
times are safe.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import Settings
from .backup_jobs import BackupJobs

logger = logging.getLogger(__name__)


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
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._tz = ZoneInfo(settings.scheduler_tz)
        self._scheduler = scheduler or AsyncIOScheduler(timezone=self._tz)

    def job_specs(self) -> list[JobSpec]:
        """The durability jobs and their schedules — pure, so it tests without a loop."""
        s = self._settings
        return [
            JobSpec("data-sync", self._jobs.run_data_sync, s.backup_data_sync_cron),
            JobSpec("db-backup", self._jobs.run_db_backup, s.backup_db_backup_cron),
            JobSpec("integrity-drill", self._jobs.run_integrity_drill, s.integrity_drill_cron),
            JobSpec("vault-sweep", self._jobs.run_vault_sweep, s.backup_vault_sweep_cron),
            JobSpec("vault-backup", self._jobs.run_vault_bundle, s.backup_vault_bundle_cron),
        ]

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
