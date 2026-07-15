"""BackupScheduler tests — job → schedule mapping (pure) + real registration (no jobs fire).

The pure ``job_specs`` map tests without an event loop; ``start()`` is exercised against a real
AsyncIOScheduler so the crontab defaults are actually parsed and the ADR-010 misfire wiring is
asserted, but the clock is never advanced so no backup job runs.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.identity.service import IdentityCapsuleService
from app.services.backup_jobs import BackupJobs
from app.services.reindex import ReindexService
from app.services.scheduler import BackupScheduler
from app.services.store_backup import StoreBackupService

from .fakes import FakeAgentRunStore, FakeGitRepo, FakeObjectStore

JOB_IDS = {"data-sync", "db-backup", "integrity-drill", "store-sweep", "store-backup"}


def _jobs(tmp_path: Path) -> tuple[Settings, BackupJobs]:
    settings = Settings(graph_store_path=str(tmp_path / "store"), scheduler_tz="UTC")
    jobs = BackupJobs(
        settings=settings,
        store=FakeAgentRunStore(),
        object_store=FakeObjectStore(),
        store_backup=StoreBackupService(settings=settings, git=FakeGitRepo()),
    )
    return settings, jobs


def _reindex(settings: Settings) -> ReindexService:
    # Only the run_scheduled coroutine is referenced by the scheduler; the deps can be None here.
    return ReindexService(
        settings=settings, indexer=None, graph=None, store_backup=None, run_store=None
    )


def test_job_specs_cover_the_five_durability_jobs(tmp_path: Path):
    settings, jobs = _jobs(tmp_path)
    specs = BackupScheduler(settings=settings, jobs=jobs).job_specs()

    assert {s.id for s in specs} == JOB_IDS
    by_id = {s.id: s.func for s in specs}
    # ids map to the intended BackupJobs coroutine methods (guards against a wiring swap).
    assert by_id["store-backup"] == jobs.run_store_bundle
    assert by_id["integrity-drill"] == jobs.run_integrity_drill
    assert by_id["db-backup"] == jobs.run_db_backup
    assert by_id["data-sync"] == jobs.run_data_sync
    assert by_id["store-sweep"] == jobs.run_store_sweep
    # every crontab default parses (a bad string would raise here).
    for s in specs:
        assert CronTrigger.from_crontab(s.crontab)


def test_reindex_job_is_added_when_a_reindex_service_is_given(tmp_path: Path):
    settings, jobs = _jobs(tmp_path)
    reindex = _reindex(settings)
    specs = BackupScheduler(settings=settings, jobs=jobs, reindex=reindex).job_specs()

    by_id = {s.id: s for s in specs}
    assert set(by_id) == JOB_IDS | {"reindex"}
    assert by_id["reindex"].func == reindex.run_scheduled
    assert by_id["reindex"].crontab == settings.reindex_cron
    assert CronTrigger.from_crontab(by_id["reindex"].crontab)


def test_reindex_job_is_absent_without_a_reindex_service(tmp_path: Path):
    settings, jobs = _jobs(tmp_path)
    specs = BackupScheduler(settings=settings, jobs=jobs).job_specs()
    assert {s.id for s in specs} == JOB_IDS  # backup-only when no reindex is wired


def _capsule(settings: Settings) -> IdentityCapsuleService:
    # Only the run_scheduled coroutine is referenced by the scheduler; the deps can be None here.
    return IdentityCapsuleService(
        settings=settings, capsule_store=None, sources=None, routing=None, run_store=None
    )


def test_identity_capsule_job_is_added_when_the_service_is_given(tmp_path: Path):
    settings, jobs = _jobs(tmp_path)
    capsule = _capsule(settings)
    specs = BackupScheduler(
        settings=settings, jobs=jobs, identity_capsule=capsule
    ).job_specs()

    by_id = {s.id: s for s in specs}
    assert set(by_id) == JOB_IDS | {"identity-capsule-refresh"}
    assert by_id["identity-capsule-refresh"].func == capsule.run_scheduled
    assert by_id["identity-capsule-refresh"].crontab == settings.identity_capsule_refresh_cron
    assert CronTrigger.from_crontab(by_id["identity-capsule-refresh"].crontab)


async def test_start_registers_all_jobs_then_shuts_down(tmp_path: Path):
    settings, jobs = _jobs(tmp_path)
    aps = AsyncIOScheduler(timezone=ZoneInfo("UTC"))
    scheduler = BackupScheduler(settings=settings, jobs=jobs, scheduler=aps)

    scheduler.start()
    try:
        assert aps.running
        assert {j.id for j in aps.get_jobs()} == JOB_IDS
        # Every job is scheduled (has a next fire time) and is a cron trigger.
        for j in aps.get_jobs():
            assert j.next_run_time is not None
            assert isinstance(j.trigger, CronTrigger)
        # ADR-010 wiring: missed fires are skipped within the grace window, never overlap,
        # and a backlog coalesces to one run.
        bundle = aps.get_job("store-backup")
        assert bundle.misfire_grace_time == settings.scheduler_misfire_grace_seconds
        assert bundle.max_instances == 1
        assert bundle.coalesce is True
    finally:
        scheduler.shutdown()  # clean teardown, must not raise


async def test_shutdown_is_safe_when_never_started(tmp_path: Path):
    settings, jobs = _jobs(tmp_path)
    scheduler = BackupScheduler(settings=settings, jobs=jobs)
    scheduler.shutdown()  # must not raise
