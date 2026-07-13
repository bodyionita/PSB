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
from .services.backup_jobs import build_backup_jobs
from .services.git_repo import GitRepo
from .services.reindex import build_reindex_service
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
# The combined reindex (ADR-023 §4) is driven by its own service, not BackupJobs.
REINDEX = "reindex"
# Every valid CLI job name (backup jobs + reindex).
JOBS: tuple[str, ...] = (*BACKUP_JOBS.keys(), REINDEX)


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
        else:
            jobs = build_backup_jobs(settings, db, store_backup)
            await getattr(jobs, BACKUP_JOBS[name])()
    finally:
        await db.disconnect()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or args[0] not in JOBS:
        sys.stderr.write(f"usage: python -m app.cli {{{'|'.join(JOBS)}}}\n")
        return 2
    asyncio.run(run_job(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
