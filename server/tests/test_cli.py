"""CLI entrypoint smoke — arg handling + job wiring, no DB connection."""

from __future__ import annotations

from pathlib import Path

import app.cli as cli
from app.cli import BACKUP_JOBS, JOBS, PIPELINES, REDERIVE_CAPTURE, REINDEX, build_backup_jobs, main
from app.config import Settings
from app.db import Database
from app.services.backup_jobs import BackupJobs
from app.services.git_repo import GitRepo
from app.services.reindex import ReindexService, build_reindex_service
from app.services.store_backup import StoreBackupService


def test_main_rejects_bad_args():
    assert main([]) == 2
    assert main(["nonsense"]) == 2
    assert main(["store-backup", "extra"]) == 2  # 2 args but not the `pipeline` verb → usage
    assert main(["pipeline"]) == 2  # `pipeline` needs a name
    assert main(["pipeline", "a", "b"]) == 2  # too many args
    assert main(["pipeline", "bogus"]) == 2  # unknown pipeline name → fast-fail (no DB setup)
    assert main([REDERIVE_CAPTURE]) == 2  # needs a <capture_id> arg → not the 1-arg JOBS path
    assert main([REDERIVE_CAPTURE, "a", "b"]) == 2  # too many args


def test_rederive_capture_dispatches_with_the_capture_id(monkeypatch):
    # `rederive-capture <id>` is a 2-arg verb (like `pipeline`): it routes to run_rederive_capture
    # with the id, not through the exactly-1-arg JOBS validation. Stub the runner so no DB connects.
    seen: dict[str, str] = {}

    async def _fake(capture_id: str) -> int:
        seen["id"] = capture_id
        return 0

    monkeypatch.setattr(cli, "run_rederive_capture", _fake)
    assert main([REDERIVE_CAPTURE, "cap-123"]) == 0
    assert seen == {"id": "cap-123"}
    assert (
        REDERIVE_CAPTURE not in JOBS
    )  # a 2-arg verb, kept out of the 1-arg JOBS set (like pipeline)


def test_pipeline_names_match_the_config():
    # `python -m app.cli pipeline <name>` accepts exactly the configured pipelines (ADR-047).
    defined = {d.name for d in Settings(scheduler_tz="UTC").pipeline_defs()}
    assert set(PIPELINES) == defined == {"nightly", "weekly"}


def test_reindex_is_a_valid_cli_job():
    assert REINDEX in JOBS  # `python -m app.cli reindex` is accepted (arg validation only)


def test_capture_pipeline_derivation_wiring_flag(tmp_path: Path):
    # The regression guard for the `rederive-capture` verb: it needs derivation wired (it re-runs
    # the VLM/STT to recover an `unavailable` item), while reprocess-all + the other CLI verbs must
    # NOT (they replay the stored raw_text, never re-running the VLM/STT). build_capture_pipeline
    # constructs its stores lazily (no DB connect), like build_reindex_service above.
    from app.services.capture_pipeline import build_capture_pipeline

    settings = Settings(graph_store_path=str(tmp_path / "store"))
    store_backup = StoreBackupService(settings=settings, git=GitRepo(settings.graph_store_path))
    db = Database(settings)

    default = build_capture_pipeline(settings, db, store_backup)  # the reprocess-all / default path
    assert default._media_store is not None  # node_media rebuild still wired (ADR-060 §3)
    assert default._media_derivation is None  # but NO derivation — no VLM/STT replay
    assert default._media_files is None

    rederive = build_capture_pipeline(settings, db, store_backup, wire_media_derivation=True)
    assert rederive._media_store is not None
    assert (
        rederive._media_derivation is not None
    )  # rederive_capture's precondition (else CaptureError)
    assert rederive._media_files is not None


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
