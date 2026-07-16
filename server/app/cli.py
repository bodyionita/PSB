"""CLI entrypoint for the scheduled jobs (ADR-014; 08 M1 build decisions; M2 reindex ADR-023).

Exposes each scheduled job as ``python -m app.cli <job>`` so a future external scheduler can drive
them without the in-process APScheduler — no rework. Builds the minimal context (db + git + R2 +
stores), runs one job, and tears down. The combined ``reindex`` (git pull → rescan → recompute
derived edges → commit+push) is here too — handy for the "DB wipe + reindex restores search"
recovery drill without going through the authenticated API.

Use the CLI **or** the in-process scheduler, not both at once: each process holds its own store
git lock, so concurrent drivers would only be serialised by git's own ``index.lock``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import get_settings
from .db import Database
from .entities.backfill import build_backfill_service
from .entities.profile_refresh import build_profile_refresh_service
from .identity.service import build_identity_capsule_service
from .services.agent_runs import PgAgentRunStore
from .services.backup_jobs import build_backup_jobs
from .services.git_repo import GitRepo
from .services.reindex import build_reindex_service
from .services.reprocess import build_reprocess_service
from .services.scheduler import PipelineScheduler
from .services.store_backup import StoreBackupService

logger = logging.getLogger(__name__)

# CLI name → BackupJobs method. Shared with the in-process scheduler (durability Slice B2).
BACKUP_JOBS: dict[str, str] = {
    "store-backup": "run_store_bundle",
    "integrity-drill": "run_integrity_drill",
    "db-backup": "run_db_backup",
    "data-sync": "run_data_sync",
    "store-sweep": "run_store_sweep",
}
# The combined reindex (ADR-023 §4) + the M3 entity jobs (ADR-030 §4/§6) each drive their own
# service, not BackupJobs.
REINDEX = "reindex"
PROFILE_REFRESH = "profile-refresh"
BACKFILL = "entity-backfill"
IDENTITY_CAPSULE = "identity-capsule-refresh"
# The chat-distiller (ADR-048, M6 task 1): distill idle chat sessions into stance-gated memories.
# Not yet a pipeline step (M6 task 8) — this standalone verb is the run-now + local-test path.
CHAT_DISTILL = "chat-distill"
# The dedup sweep (ADR-049, M6 task 5): file dedup-proposal review items for near-duplicate content
# nodes. Not yet a pipeline step (M6 task 8) — standalone verb = the run-now + local-test path.
DEDUP_SWEEP = "dedup-sweep"
# The inbox drainer (ADR-048 §10, M6 task 6): re-organize `inbox/`-materialized captures against the
# now-richer registry. A `nightly` pipeline step (M6 task 8); this standalone verb = run-now + test.
INBOX_DRAIN = "inbox-drain"
# The weekly maybe-digest (ADR-048 §8, M6 task 8): a feed-visible run summarizing parked `maybe`
# review items. A `weekly` pipeline step; this standalone verb = the run-now + local-test path.
MAYBE_DIGEST = "maybe-digest"
# The reprocess-all-from-raw op (ADR-042). Destructive of derived state but confirm is implicit at
# the CLI (an operator running it deliberately) — raw + approved vocab are preserved.
REPROCESS = "reprocess-all"
# Every valid CLI job name (backup jobs + reindex + entity jobs + capsule + distill + reprocess).
JOBS: tuple[str, ...] = (
    *BACKUP_JOBS.keys(),
    REINDEX,
    PROFILE_REFRESH,
    BACKFILL,
    IDENTITY_CAPSULE,
    CHAT_DISTILL,
    DEDUP_SWEEP,
    INBOX_DRAIN,
    MAYBE_DIGEST,
    REPROCESS,
)


async def run_pipeline(name: str) -> int:
    """Run one whole pipeline (``nightly``/``weekly``) once, on demand — the ADR-047 run-now path.

    Builds every step's service from the same ``build_*`` helpers the per-job CLI + the app use,
    hands them to a :class:`PipelineScheduler` (never started — we only borrow its runner wiring),
    and drives the chosen pipeline's :class:`PipelineRunner` to completion in this process. Prints
    the parent run id + each step's status + child run id, so a VPS operator gets self-contained
    evidence that one start drove the whole roster in order under a parent run (M5.5 Accept). Every
    step is idempotent (rule 6); this does the same work the nightly cron would — reindex included.
    """
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    try:
        store_backup = StoreBackupService(settings=settings, git=GitRepo(settings.graph_store_path))
        await store_backup.ensure_ready()
        # Effective vocabulary (seeds ∪ approved additions) so profile-refresh/backfill scan the
        # same entity set the in-app nightly does (ADR-027 forward-live) — otherwise run-now would
        # be seeds-only and under-count when a governance type was approved. Built as a provider.
        from .vocab.service import VocabularyService
        from .vocab.store import PgVocabularyStore

        vocab = VocabularyService(settings=settings, vocab_store=PgVocabularyStore(db))
        # M6 sleep-cycle steps (ADR-048) join the roster here. The chat-distiller (endorsed →
        # capture → organizer) and the inbox-drain (reorganize_capture) both drive the SAME pipeline
        # — the single writer (rule 2b) — so they organize into one store; we drain it + flush the
        # store backup after the run so this short-lived process commits their background work (the
        # in-app nightly's long-lived debounce owns that itself — the reprocess-all/CLI pattern).
        from .chat.distiller import build_chat_distiller_service
        from .inbox.drain import InboxDrainService
        from .services.capture_pipeline import build_capture_pipeline
        from .services.capture_store import PgCaptureStore
        from .services.maybe_digest import build_maybe_digest_service

        pipeline = build_capture_pipeline(settings, db, store_backup)
        from .dedup.sweep import build_dedup_sweep_service

        scheduler = PipelineScheduler(
            settings=settings,
            jobs=build_backup_jobs(settings, db, store_backup),
            run_store=PgAgentRunStore(db),
            reindex=build_reindex_service(settings, db, store_backup),
            profile_refresh=build_profile_refresh_service(settings, db, vocab),
            backfill=build_backfill_service(settings, db, store_backup, vocab),
            identity_capsule=build_identity_capsule_service(settings, db),
            chat_distiller=build_chat_distiller_service(settings, db, pipeline),
            inbox_drain=InboxDrainService(
                settings=settings,
                capture_store=PgCaptureStore(db),
                pipeline=pipeline,
                run_store=PgAgentRunStore(db),
            ),
            dedup_sweep=build_dedup_sweep_service(settings, db, vocab),
            maybe_digest=build_maybe_digest_service(settings, db),
        )
        runners = {defn.name: runner for defn, runner in scheduler.pipeline_runners()}
        if name not in runners:
            sys.stderr.write(
                f"unknown pipeline {name!r}; known: {', '.join(sorted(runners))}\n"
            )
            return 2
        outcome = await runners[name].run()
        # Drain the shared capture pipeline (background organizes from chat-distill / inbox-drain)
        # then flush the store backup so this one-shot process commits the resolved nodes before it
        # exits — the in-app nightly relies on its long-lived debounce instead (rule 6, idempotent).
        await pipeline.drain()
        await store_backup.backup_now(f"pipeline-{name}")
        if outcome is None:
            logger.error("pipeline %s did not run (could not open its parent run)", name)
            return 1
        logger.info("%s", outcome.summary())
        logger.info("parent run: %s", outcome.parent_run_id)
        for s in outcome.steps:
            logger.info("  step %-26s %-9s child_run=%s", s.name, s.status, s.child_run_id)
        # A halt-aborted pipeline is a non-zero exit; a continue-only failure still exits 0 (the
        # pipeline completed its roster — per-step failures are visible in the runs, ADR-047 §4).
        return 1 if outcome.halted_at is not None else 0
    finally:
        await db.disconnect()


async def run_job(name: str) -> None:
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    try:
        # Standalone run: make sure the repo is initialised/pinned before a job touches git.
        store_backup = StoreBackupService(settings=settings, git=GitRepo(settings.graph_store_path))
        await store_backup.ensure_ready()
        if name == REINDEX:
            await build_reindex_service(settings, db, store_backup).run_scheduled()
        elif name == REPROCESS:
            # Run the pass to completion in this process (apply spawns it in the background; drain
            # awaits it) — the sanctioned path for the pre-prod local dry-run (ADR-042).
            service = build_reprocess_service(settings, db, store_backup)
            await service.apply()
            await service.drain()
        elif name == PROFILE_REFRESH:
            await build_profile_refresh_service(settings, db).run_scheduled()
        elif name == BACKFILL:
            await build_backfill_service(settings, db, store_backup).run_scheduled()
        elif name == IDENTITY_CAPSULE:
            # DB-only (app_settings + reads), no store git — like profile-refresh.
            await build_identity_capsule_service(settings, db).run_scheduled()
        elif name == CHAT_DISTILL:
            # Endorsed candidates go through the organizer (single writer, rule 2b), so the
            # distiller needs a real capture pipeline. Run it, then DRAIN the pipeline so the
            # background organizes finish before this short-lived process exits.
            from .chat.distiller import build_chat_distiller_service
            from .services.capture_pipeline import build_capture_pipeline

            pipeline = build_capture_pipeline(settings, db, store_backup)
            await build_chat_distiller_service(settings, db, pipeline).run_scheduled()
            await pipeline.drain()
        elif name == DEDUP_SWEEP:
            # DB-only (candidate reads + review-queue writes, no store git) — like profile-refresh.
            from .dedup.sweep import build_dedup_sweep_service

            await build_dedup_sweep_service(settings, db).run_scheduled()
        elif name == INBOX_DRAIN:
            # Re-organize goes through the single writer (rule 2b), so the drainer drives a real
            # capture pipeline. Run it, then flush the store backup so this one-shot commits the
            # resolved nodes (the in-app nightly's long-lived debounce handles that itself; a CLI
            # process exits too soon — the reprocess-all pattern).
            from .inbox.drain import build_inbox_drain_service

            await build_inbox_drain_service(settings, db, store_backup).run_scheduled()
            await store_backup.backup_now("inbox-drain")
        elif name == MAYBE_DIGEST:
            # DB-only (a read over review_queue + its own run row, no store git) — like the sweep.
            from .services.maybe_digest import build_maybe_digest_service

            await build_maybe_digest_service(settings, db).run_scheduled()
        else:
            jobs = build_backup_jobs(settings, db, store_backup)
            await getattr(jobs, BACKUP_JOBS[name])()
    finally:
        await db.disconnect()


# The two schedulable pipelines (ADR-047) — run a whole roster once with `python -m app.cli
# pipeline <name>`. The names must match the config `pipeline_defs()`.
PIPELINES: tuple[str, ...] = ("nightly", "weekly")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    # `pipeline <name>` runs a whole pipeline once (ADR-047 run-now); everything else is one job.
    # Validate the name up front so a typo fails fast, before any DB connect / store git init.
    if len(args) == 2 and args[0] == "pipeline":
        if args[1] not in PIPELINES:
            sys.stderr.write(f"usage: python -m app.cli pipeline {{{'|'.join(PIPELINES)}}}\n")
            return 2
        return asyncio.run(run_pipeline(args[1]))
    if len(args) != 1 or args[0] not in JOBS:
        sys.stderr.write(
            f"usage: python -m app.cli {{{'|'.join(JOBS)}}}\n"
            f"       python -m app.cli pipeline {{{'|'.join(PIPELINES)}}}\n"
        )
        return 2
    asyncio.run(run_job(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
