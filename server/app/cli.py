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
# The entity-hub dedup detector (ADR-064 §4, M9.8 T4): surface duplicate hubs (inline high-conf
# + `entity-dedup` review items for low). A `nightly` pipeline step; this standalone verb = run-now.
ENTITY_DEDUP = "entity-dedup"
# The inbox drainer (ADR-048 §10, M6 task 6): re-organize `inbox/`-materialized captures against the
# now-richer registry. A `nightly` pipeline step (M6 task 8); this standalone verb = run-now + test.
INBOX_DRAIN = "inbox-drain"
# The weekly maybe-digest (ADR-048 §8, M6 task 8): a feed-visible run summarizing parked `maybe`
# review items. A `weekly` pipeline step; this standalone verb = the run-now + local-test path.
MAYBE_DIGEST = "maybe-digest"
# The reprocess-all-from-raw op (ADR-042). Destructive of derived state but confirm is implicit at
# the CLI (an operator running it deliberately) — raw + approved vocab are preserved.
REPROCESS = "reprocess-all"
# The legacy-voice → media backfill (ADR-060 §5): relocate pre-substrate voice audio into the media
# layout, mint `voice` media rows, link node_media. Idempotent + degrading; T6 runs it at deploy.
VOICE_MEDIA_BACKFILL = "voice-media-backfill"
# Targeted media re-derive → node recovery (ADR-060 §5), taking one `<capture_id>` argument. The
# operator/drill trigger for `CapturePipeline.rederive_capture`: re-run the VLM/STT on an
# `unavailable` image/voice capture, refresh its `raw_text`, reorganize so the recovered text
# reaches the NODE (not just `GET /media/{id}`). The M9 T6 failure→placeholder→re-derive drill's
# live path (no HTTP trigger exists yet — the connector re-derive endpoint lands at M9.5); parallels
# `reindex`/`reprocess-all` having a CLI verb for the recovery drill without the authenticated API.
REDERIVE_CAPTURE = "rederive-capture"
# Every valid CLI job name (backup jobs + reindex + entity jobs + capsule + distill + reprocess).
JOBS: tuple[str, ...] = (
    *BACKUP_JOBS.keys(),
    REINDEX,
    PROFILE_REFRESH,
    BACKFILL,
    IDENTITY_CAPSULE,
    CHAT_DISTILL,
    DEDUP_SWEEP,
    ENTITY_DEDUP,
    INBOX_DRAIN,
    MAYBE_DIGEST,
    REPROCESS,
    VOICE_MEDIA_BACKFILL,
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
        from .dedup.sweep import build_dedup_sweep_service
        from .entities.entity_dedup import build_entity_dedup_service
        from .inbox.drain import InboxDrainService
        from .services.capture_pipeline import build_capture_pipeline
        from .services.capture_store import PgCaptureStore
        from .services.graph_health import build_graph_health_service
        from .services.maybe_digest import build_maybe_digest_service
        from .services.occurred_enrichment import build_occurred_enrichment_service

        pipeline = build_capture_pipeline(settings, db, store_backup)
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
            entity_dedup=build_entity_dedup_service(settings, db, vocab),
            maybe_digest=build_maybe_digest_service(settings, db),
            # Read-mostly nightly-tail reporters (M8/M8.2) — wired so `pipeline nightly` faithfully
            # runs the whole roster the cron does (this method's contract), not a subset.
            graph_health=build_graph_health_service(settings, db),
            occurred_enrichment=build_occurred_enrichment_service(settings, db),
        )
        runners = {defn.name: runner for defn, runner in scheduler.pipeline_runners()}
        if name not in runners:
            sys.stderr.write(f"unknown pipeline {name!r}; known: {', '.join(sorted(runners))}\n")
            return 2
        outcome = await runners[name].run()
        # Only the distiller + inbox-drain organize through the shared capture pipeline; drain it +
        # flush the store backup solely when this pipeline actually ran one of them, so a one-shot
        # process commits their background work before it exits (the in-app nightly relies on its
        # long-lived debounce instead; rule 6, idempotent). A pipeline without them (e.g. `weekly`)
        # skips this — no empty drain, no spurious `pipeline-<name>` backup commit/log.
        ran = {s.name for s in outcome.steps} if outcome is not None else set()
        if ran & {"chat-distiller", "inbox-drain"}:
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
        elif name == ENTITY_DEDUP:
            # DB-only (hub reads + review-queue writes / its own run row, no store git) — like the
            # dedup sweep. Never auto-merges; it only surfaces candidates (ADR-064 §4).
            from .entities.entity_dedup import build_entity_dedup_service

            await build_entity_dedup_service(settings, db).run_scheduled()
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
        elif name == VOICE_MEDIA_BACKFILL:
            # Relocate legacy voice audio → mint media rows → link node_media (ADR-060 §5). DB + the
            # /srv/data media volume (R2-synced), no git store — idempotent + degrading. Deploy op.
            from .services.media_backfill import build_voice_media_backfill_service

            await (await build_voice_media_backfill_service(settings, db)).run()
        else:
            jobs = build_backup_jobs(settings, db, store_backup)
            await getattr(jobs, BACKUP_JOBS[name])()
    finally:
        await db.disconnect()


async def run_rederive_capture(capture_id: str) -> int:
    """Re-derive one media capture and recover its node (ADR-060 §5) — the T6 recovery drill.

    Builds a capture pipeline **with derivation wired** (``wire_media_derivation=True`` — unlike
    reprocess, this must re-run the VLM/STT to recover an ``unavailable`` item), awaits
    :meth:`CapturePipeline.rederive_capture` (re-derive media → refresh ``raw_text`` → reorganize),
    then drains + flushes the store backup so this one-shot process commits the recovered node
    before exiting (the inbox-drain / reprocess CLI pattern; the in-app path relies on its
    long-lived debounce). Idempotent (rule 6) — a still-``unavailable`` re-derive just re-writes
    the placeholder. Returns 1 with a message on an unknown / non-media capture, not a traceback."""
    from .services.capture_pipeline import CaptureError, build_capture_pipeline

    settings = get_settings()
    db = Database(settings)
    await db.connect()
    try:
        store_backup = StoreBackupService(settings=settings, git=GitRepo(settings.graph_store_path))
        await store_backup.ensure_ready()
        pipeline = build_capture_pipeline(settings, db, store_backup, wire_media_derivation=True)
        try:
            await pipeline.rederive_capture(capture_id)
        except CaptureError as exc:
            sys.stderr.write(f"rederive-capture: {exc}\n")
            return 1
        await pipeline.drain()
        # Reorganize wrote the (re)organized node to the store; commit it before this process exits.
        await store_backup.backup_now("rederive-capture")
        # "complete", not "recovered": a still-`unavailable` re-derive is a valid no-op re-filing
        # the placeholder. The resulting media status is in the derivation/reorganize agent_runs
        # (rule 7) + SQL smoke block 2 — the drill verifies actual recovery there, not by exit code.
        logger.info("rederive-capture: re-derive complete for capture %s", capture_id)
        return 0
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
    # `rederive-capture <capture_id>` — a 2-arg verb (like `pipeline`), the arg being an arbitrary
    # capture id, so it's validated by arity + name here, not the exactly-1-arg JOBS check below.
    if len(args) == 2 and args[0] == REDERIVE_CAPTURE:
        return asyncio.run(run_rederive_capture(args[1]))
    if len(args) != 1 or args[0] not in JOBS:
        sys.stderr.write(
            f"usage: python -m app.cli {{{'|'.join(JOBS)}}}\n"
            f"       python -m app.cli pipeline {{{'|'.join(PIPELINES)}}}\n"
            f"       python -m app.cli {REDERIVE_CAPTURE} <capture_id>\n"
        )
        return 2
    asyncio.run(run_job(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
