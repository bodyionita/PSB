"""CLI entrypoint for the durability jobs (ADR-014; 08 M1 build decisions).

Exposes each scheduled backup job as ``python -m app.cli <job>`` so a future external scheduler can
drive them without the in-process APScheduler — no rework. Builds the minimal context (db + git +
R2 + stores), runs one job, and tears down.

Use the CLI **or** the in-process scheduler, not both at once: each process holds its own vault
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
from .services.vault_backup import VaultBackupService

logger = logging.getLogger(__name__)

# CLI name → BackupJobs method. Shared with the in-process scheduler (durability Slice B2).
JOBS: dict[str, str] = {
    "vault-backup": "run_vault_bundle",
    "integrity-drill": "run_integrity_drill",
    "db-backup": "run_db_backup",
    "data-sync": "run_data_sync",
    "vault-sweep": "run_vault_sweep",
}


async def run_job(name: str) -> None:
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    try:
        # Standalone run: make sure the repo is initialised/pinned before a job touches git.
        vault_backup = VaultBackupService(settings=settings, git=GitRepo(settings.vault_path))
        await vault_backup.ensure_ready()
        jobs = build_backup_jobs(settings, db, vault_backup)
        await getattr(jobs, JOBS[name])()
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
