"""CLI entrypoint smoke — arg handling + job wiring, no DB connection."""

from __future__ import annotations

from pathlib import Path

from app.cli import JOBS, build_backup_jobs, main
from app.config import Settings
from app.db import Database
from app.services.backup_jobs import BackupJobs
from app.services.git_repo import GitRepo
from app.services.vault_backup import VaultBackupService


def test_main_rejects_bad_args():
    assert main([]) == 2
    assert main(["nonsense"]) == 2
    assert main(["vault-backup", "extra"]) == 2


def test_jobs_map_to_real_methods(tmp_path: Path):
    # build_backup_jobs constructs without touching the DB pool (no connect()).
    settings = Settings(vault_path=str(tmp_path / "vault"))
    vault_backup = VaultBackupService(settings=settings, git=GitRepo(settings.vault_path))
    jobs = build_backup_jobs(settings, Database(settings), vault_backup)
    assert isinstance(jobs, BackupJobs)
    for method_name in JOBS.values():
        assert callable(getattr(jobs, method_name))
