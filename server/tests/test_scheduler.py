"""PipelineScheduler tests — one cron per pipeline (ADR-047), pure runner mapping + real
registration (no pipelines fire).

The pure ``pipeline_runners`` map tests without an event loop: it asserts the two pipelines, their
step→job wiring, and that an unwired optional job is dropped from a pipeline's steps (not scheduled
as a doomed ``missing`` step). ``start()`` is exercised against a real AsyncIOScheduler so the
pipeline crons are actually parsed and the ADR-010 misfire/overlap wiring is asserted, but the clock
is never advanced so nothing runs.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.services.backup_jobs import BackupJobs
from app.services.scheduler import PipelineScheduler
from app.services.store_backup import StoreBackupService

from .fakes import FakeAgentRunStore, FakeGitRepo, FakeObjectStore

# The full nightly roster (all optional jobs wired — the prod shape) + the weekly pipeline. The M6
# sleep-cycle jobs (ADR-048) are woven into their dependency slots: chat-distiller first, then
# inbox-drain before reindex, and dedup-sweep after the entity jobs.
NIGHTLY_STEPS = [
    "chat-distiller",
    "data-sync",
    "db-backup",
    "inbox-drain",
    "reindex",
    "profile-refresh",
    "entity-backfill",
    "identity-capsule-refresh",
    "dedup-sweep",
    "store-sweep",
    "store-backup",
]
WEEKLY_STEPS = ["integrity-drill", "maybe-digest"]
# With no optional agent-window / sleep-cycle jobs wired, only the five backup steps survive (two of
# them weekly) — the M6 steps drop with a log, never scheduled as doomed `missing` steps.
BACKUP_ONLY_NIGHTLY = ["data-sync", "db-backup", "store-sweep", "store-backup"]


class _FakeJob:
    """A stand-in agent-window job — the scheduler only reads its ``run_scheduled`` for the map."""

    async def run_scheduled(self) -> None:  # pragma: no cover - never fired in these tests
        return None


def _jobs(tmp_path: Path) -> tuple[Settings, BackupJobs]:
    settings = Settings(graph_store_path=str(tmp_path / "store"), scheduler_tz="UTC")
    jobs = BackupJobs(
        settings=settings,
        store=FakeAgentRunStore(),
        object_store=FakeObjectStore(),
        store_backup=StoreBackupService(settings=settings, git=FakeGitRepo()),
    )
    return settings, jobs


def _full_scheduler(tmp_path: Path, **over) -> PipelineScheduler:
    """A scheduler with every optional agent-window + M6 sleep-cycle job wired (the prod shape)."""
    settings, jobs = _jobs(tmp_path)
    return PipelineScheduler(
        settings=settings,
        jobs=jobs,
        run_store=FakeAgentRunStore(),
        reindex=_FakeJob(),
        profile_refresh=_FakeJob(),
        backfill=_FakeJob(),
        identity_capsule=_FakeJob(),
        chat_distiller=_FakeJob(),
        inbox_drain=_FakeJob(),
        dedup_sweep=_FakeJob(),
        maybe_digest=_FakeJob(),
        **over,
    )


def test_pipeline_runners_cover_nightly_and_weekly_in_order(tmp_path: Path):
    runners = _full_scheduler(tmp_path).pipeline_runners()
    by_name = {d.name: d for d, _ in runners}

    assert set(by_name) == {"nightly", "weekly"}
    # steps preserved in dependency order (the migrated ADR-010 roster + the M6 sleep-cycle jobs).
    assert [s.name for s in by_name["nightly"].steps] == NIGHTLY_STEPS
    assert [s.name for s in by_name["weekly"].steps] == WEEKLY_STEPS
    # every pipeline cron parses.
    for defn, _ in runners:
        assert CronTrigger.from_crontab(defn.cron)


def test_job_runner_is_threaded_into_every_runner(tmp_path: Path):
    # M8 (ADR-053 §7): the shared single-flight guard reaches each PipelineRunner so a nightly step
    # and a manual run of the same agent can't overlap.
    from app.services.job_runner import JobRunner

    guard = JobRunner()
    runners = _full_scheduler(tmp_path, job_runner=guard).pipeline_runners()
    assert runners  # sanity
    assert all(runner._job_runner is guard for _, runner in runners)


def test_step_funcs_map_to_the_intended_job_coroutines(tmp_path: Path):
    settings, jobs = _jobs(tmp_path)
    reindex, profile, backfill, capsule = _FakeJob(), _FakeJob(), _FakeJob(), _FakeJob()
    distiller, drain, dedup, digest = _FakeJob(), _FakeJob(), _FakeJob(), _FakeJob()
    scheduler = PipelineScheduler(
        settings=settings,
        jobs=jobs,
        run_store=FakeAgentRunStore(),
        reindex=reindex,
        profile_refresh=profile,
        backfill=backfill,
        identity_capsule=capsule,
        chat_distiller=distiller,
        inbox_drain=drain,
        dedup_sweep=dedup,
        maybe_digest=digest,
    )
    funcs = scheduler._step_funcs()
    # backup jobs → BackupJobs methods; guards against a wiring swap.
    assert funcs["data-sync"] == jobs.run_data_sync
    assert funcs["db-backup"] == jobs.run_db_backup
    assert funcs["store-sweep"] == jobs.run_store_sweep
    assert funcs["store-backup"] == jobs.run_store_bundle
    assert funcs["integrity-drill"] == jobs.run_integrity_drill
    # agent-window jobs → their run_scheduled.
    assert funcs["reindex"] == reindex.run_scheduled
    assert funcs["profile-refresh"] == profile.run_scheduled
    assert funcs["entity-backfill"] == backfill.run_scheduled
    assert funcs["identity-capsule-refresh"] == capsule.run_scheduled
    # M6 sleep-cycle jobs → their run_scheduled (ADR-048).
    assert funcs["chat-distiller"] == distiller.run_scheduled
    assert funcs["inbox-drain"] == drain.run_scheduled
    assert funcs["dedup-sweep"] == dedup.run_scheduled
    assert funcs["maybe-digest"] == digest.run_scheduled


def test_unwired_optional_jobs_are_dropped_from_the_pipeline(tmp_path: Path):
    # No reindex/profile/backfill/capsule wired: those steps must be OMITTED (not recorded as a
    # failed `missing` step every night — ADR-047 §5), leaving only the backup steps.
    settings, jobs = _jobs(tmp_path)
    scheduler = PipelineScheduler(settings=settings, jobs=jobs, run_store=FakeAgentRunStore())
    by_name = {d.name: d for d, _ in scheduler.pipeline_runners()}

    assert [s.name for s in by_name["nightly"].steps] == BACKUP_ONLY_NIGHTLY
    assert [s.name for s in by_name["weekly"].steps] == ["integrity-drill"]  # jobs always wired


def test_a_pipeline_with_no_wired_steps_is_skipped(tmp_path: Path):
    # A pipeline whose every step is unwired must not be scheduled at all. Build a settings whose
    # weekly pipeline references only an unknown job.
    class _OnlyGhostWeekly(Settings):
        def pipeline_defs(self):
            from app.services.pipeline import CONTINUE, PipelineDef, PipelineStepDef

            return (
                PipelineDef(
                    name="weekly",
                    cron=self.weekly_pipeline_cron,
                    steps=(PipelineStepDef("ghost-job", on_fail=CONTINUE),),
                ),
            )

    settings = _OnlyGhostWeekly(graph_store_path=str(tmp_path / "store"), scheduler_tz="UTC")
    _, jobs = _jobs(tmp_path)
    scheduler = PipelineScheduler(settings=settings, jobs=jobs, run_store=FakeAgentRunStore())
    assert scheduler.pipeline_runners() == []  # nothing schedulable


async def test_start_registers_one_cron_per_pipeline_then_shuts_down(tmp_path: Path):
    aps = AsyncIOScheduler(timezone=ZoneInfo("UTC"))
    scheduler = _full_scheduler(tmp_path, scheduler=aps)

    scheduler.start()
    try:
        assert aps.running
        # exactly one APScheduler job per pipeline (not per step/job).
        assert {j.id for j in aps.get_jobs()} == {"nightly", "weekly"}
        for j in aps.get_jobs():
            assert j.next_run_time is not None
            assert isinstance(j.trigger, CronTrigger)
        settings, _ = _jobs(tmp_path)
        # ADR-010 / ADR-047 §3 wiring: missed fires skipped within grace, one run coalesced, and a
        # pipeline never overlaps itself (so a long night can't stack on the next).
        nightly = aps.get_job("nightly")
        assert nightly.misfire_grace_time == settings.scheduler_misfire_grace_seconds
        assert nightly.max_instances == 1
        assert nightly.coalesce is True
    finally:
        scheduler.shutdown()  # clean teardown, must not raise


async def test_shutdown_is_safe_when_never_started(tmp_path: Path):
    scheduler = _full_scheduler(tmp_path)
    scheduler.shutdown()  # must not raise
