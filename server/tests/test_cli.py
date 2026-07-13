"""CLI entrypoint smoke — arg handling + job wiring, no DB connection."""

from __future__ import annotations

from pathlib import Path

from app.cli import BACKUP_JOBS, JOBS, REINDEX, build_backup_jobs, main
from app.config import Settings
from app.db import Database
from app.services.backup_jobs import BackupJobs
from app.services.git_repo import GitRepo
from app.services.reindex import ReindexService, build_reindex_service
from app.services.store_backup import StoreBackupService


def test_main_rejects_bad_args():
    assert main([]) == 2
    assert main(["nonsense"]) == 2
    assert main(["store-backup", "extra"]) == 2


def test_reindex_is_a_valid_cli_job():
    assert REINDEX in JOBS  # `python -m app.cli reindex` is accepted (arg validation only)


def test_backup_jobs_map_to_real_methods(tmp_path: Path):
    # build_backup_jobs constructs without touching the DB pool (no connect()).
    settings = Settings(graph_store_path=str(tmp_path / "store"))
    store_backup = StoreBackupService(settings=settings, git=GitRepo(settings.graph_store_path))
    jobs = build_backup_jobs(settings, Database(settings), store_backup)
    assert isinstance(jobs, BackupJobs)
    for method_name in BACKUP_JOBS.values():
        assert callable(getattr(jobs, method_name))


def test_build_reindex_service_constructs_without_a_db_connection(tmp_path: Path):
    # Mirrors build_backup_jobs: composes indexer/graph/registry lazily, no pool connect().
    settings = Settings(graph_store_path=str(tmp_path / "store"))
    store_backup = StoreBackupService(settings=settings, git=GitRepo(settings.graph_store_path))
    service = build_reindex_service(settings, Database(settings), store_backup)
    assert isinstance(service, ReindexService)
