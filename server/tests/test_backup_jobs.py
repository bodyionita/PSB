"""BackupJobs tests — fakes for R2/agent-runs/git, no network, no real git, no DB.

Each job must record an agent_runs row (succeeded / failed / skipped) and never raise (rule 7).
The real `git bundle` round-trip is integration-tested in test_git_repo.py.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.services.agent_runs import AgentRun
from app.services.backup_jobs import BackupJobs
from app.services.store_backup import Fingerprint, StoreBackupService

from .fakes import FakeAgentRunStore, FakeGitRepo, FakeObjectStore


def _jobs(
    tmp_path: Path,
    *,
    object_store=None,
    git=None,
    store=None,
    db_dumper=None,
    bundle_inspector=None,
):
    settings = Settings(
        graph_store_path=str(tmp_path / "store"),
        data_path=str(tmp_path / "data"),
        scheduler_tz="UTC",
    )
    git = git or FakeGitRepo()
    store = store or FakeAgentRunStore()
    store_backup = StoreBackupService(settings=settings, git=git)
    jobs = BackupJobs(
        settings=settings,
        store=store,
        object_store=object_store,
        store_backup=store_backup,
        db_dumper=db_dumper,
        bundle_inspector=bundle_inspector,
    )
    return jobs, store, git


async def test_store_bundle_uploads_and_records_fingerprint(tmp_path: Path):
    obj = FakeObjectStore()
    jobs, store, _ = _jobs(tmp_path, object_store=obj)
    await jobs.run_store_bundle()

    run = store.runs["run-1"]
    assert run.agent == "store-backup" and run.status == "succeeded"
    assert run.details["commit_count"] == 3 and run.details["head_sha"] == "deadbeef"
    assert any(k.startswith("store/bundle-") and k.endswith(".bundle") for k in obj.objects)
    assert any(k.endswith(".manifest.json") for k in obj.objects)


async def test_store_bundle_fails_on_commit_count_regression(tmp_path: Path):
    # A second bundle whose commit count DROPPED below the last good one must fail (the
    # rewrite/truncation alarm), so it never becomes the new monotonic baseline (ADR-014 §6).
    obj = FakeObjectStore()
    git = FakeGitRepo()
    git.commit_count_value = 5
    jobs, store, git = _jobs(tmp_path, object_store=obj, git=git)
    await jobs.run_store_bundle()
    assert store.runs["run-1"].status == "succeeded"  # baseline = 5 commits

    git.commit_count_value = 3  # history shrank
    await jobs.run_store_bundle()
    run = store.runs["run-2"]
    assert run.status == "failed" and "regressed" in (run.error or "")


async def test_r2_job_survives_agent_run_open_failure(tmp_path: Path):
    # If opening the agent_runs row fails (DB down), the job logs + bails, never raising (rule 7).
    class _StartFails(FakeAgentRunStore):
        async def start(self, agent: str) -> str:
            raise RuntimeError("db down")

    jobs, _, _ = _jobs(tmp_path, object_store=FakeObjectStore(), store=_StartFails())
    await jobs.run_db_backup()  # must not raise


async def test_r2_jobs_skip_when_backups_disabled(tmp_path: Path):
    jobs, store, _ = _jobs(tmp_path, object_store=None)
    await jobs.run_store_bundle()
    await jobs.run_db_backup()
    assert store.runs["run-1"].status == "skipped"
    assert store.runs["run-2"].status == "skipped"


async def test_db_backup_uploads_dump(tmp_path: Path):
    obj = FakeObjectStore()

    async def dumper() -> bytes:
        return b"-- SQL DUMP"

    jobs, store, _ = _jobs(tmp_path, object_store=obj, db_dumper=dumper)
    await jobs.run_db_backup()

    assert store.runs["run-1"].status == "succeeded"
    key = next(k for k in obj.objects if k.startswith("db/pg_dump-"))
    assert obj.objects[key] == b"-- SQL DUMP"


async def test_db_backup_failure_marks_run_failed(tmp_path: Path):
    async def dumper() -> bytes:
        raise RuntimeError("pg_dump not found")

    jobs, store, _ = _jobs(tmp_path, object_store=FakeObjectStore(), db_dumper=dumper)
    await jobs.run_db_backup()  # must not raise (rule 7)

    run = store.runs["run-1"]
    assert run.status == "failed" and "pg_dump not found" in (run.error or "")


async def test_data_sync_uploads_each_file(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.m4a").write_bytes(b"x")
    (data / "b.wav").write_bytes(b"yy")
    obj = FakeObjectStore()
    jobs, store, _ = _jobs(tmp_path, object_store=obj)
    await jobs.run_data_sync()

    run = store.runs["run-1"]
    assert run.status == "succeeded"
    assert run.details == {"uploaded": 2, "skipped": 0, "total": 2}
    assert obj.objects["data/a.m4a"] == b"x" and obj.objects["data/b.wav"] == b"yy"


async def test_data_sync_skips_already_uploaded_files(tmp_path: Path):
    # WORM-safe: an object already in R2 (immutable, locked) is never re-PUT (ADR-014).
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.m4a").write_bytes(b"x")
    (data / "b.wav").write_bytes(b"yy")
    obj = FakeObjectStore()
    obj.objects["data/a.m4a"] = b"x"  # pretend a.m4a was uploaded on a prior run
    jobs, store, _ = _jobs(tmp_path, object_store=obj)
    await jobs.run_data_sync()

    run = store.runs["run-1"]
    assert run.status == "succeeded"
    assert run.details == {"uploaded": 1, "skipped": 1, "total": 2}
    assert set(obj.objects) == {"data/a.m4a", "data/b.wav"}  # only the new file added


async def test_data_sync_no_data_dir_is_zero(tmp_path: Path):
    jobs, store, _ = _jobs(tmp_path, object_store=FakeObjectStore())
    await jobs.run_data_sync()
    assert store.runs["run-1"].status == "succeeded"
    assert store.runs["run-1"].details == {"uploaded": 0, "skipped": 0, "total": 0}


def _preloaded_manifest(store: FakeAgentRunStore, manifest: dict) -> None:
    store.preloaded["store-backup"] = AgentRun(
        id="prev", agent="store-backup", status="succeeded", details=manifest
    )


async def test_integrity_drill_succeeds_when_fingerprints_match(tmp_path: Path):
    manifest = {
        "head_sha": "abc",
        "commit_count": 3,
        "file_count": 5,
        "key": "store/bundle-x.bundle",
        "bytes": 10,
    }
    obj = FakeObjectStore()
    obj.objects[manifest["key"]] = b"BUNDLE"
    store = FakeAgentRunStore()
    _preloaded_manifest(store, manifest)
    git = FakeGitRepo()
    git.commit_count_value = 3  # live == manifest (monotonic ok)

    async def inspector(data: bytes) -> Fingerprint:
        return Fingerprint(head_sha="abc", commit_count=3, file_count=5)

    jobs, store, _ = _jobs(
        tmp_path, object_store=obj, git=git, store=store, bundle_inspector=inspector
    )
    await jobs.run_integrity_drill()
    assert store.runs["run-1"].status == "succeeded"


async def test_integrity_drill_flags_commit_count_regression(tmp_path: Path):
    manifest = {
        "head_sha": "abc",
        "commit_count": 3,
        "file_count": 5,
        "key": "store/bundle-x.bundle",
        "bytes": 10,
    }
    obj = FakeObjectStore()
    obj.objects[manifest["key"]] = b"BUNDLE"
    store = FakeAgentRunStore()
    _preloaded_manifest(store, manifest)
    git = FakeGitRepo()
    git.commit_count_value = 2  # live < manifest → rewrite/truncation alarm

    async def inspector(data: bytes) -> Fingerprint:
        return Fingerprint(head_sha="abc", commit_count=3, file_count=5)

    jobs, store, _ = _jobs(
        tmp_path, object_store=obj, git=git, store=store, bundle_inspector=inspector
    )
    await jobs.run_integrity_drill()
    run = store.runs["run-1"]
    assert run.status == "failed" and "regressed" in (run.error or "")


async def test_integrity_drill_flags_bundle_mismatch(tmp_path: Path):
    manifest = {
        "head_sha": "abc",
        "commit_count": 3,
        "file_count": 5,
        "key": "store/bundle-x.bundle",
        "bytes": 10,
    }
    obj = FakeObjectStore()
    obj.objects[manifest["key"]] = b"BUNDLE"
    store = FakeAgentRunStore()
    _preloaded_manifest(store, manifest)

    async def inspector(data: bytes) -> Fingerprint:
        return Fingerprint(head_sha="abc", commit_count=99, file_count=5)  # ≠ manifest

    jobs, store, _ = _jobs(tmp_path, object_store=obj, store=store, bundle_inspector=inspector)
    await jobs.run_integrity_drill()
    run = store.runs["run-1"]
    assert run.status == "failed" and "commit-count" in (run.error or "")


async def test_integrity_drill_fails_without_a_bundle(tmp_path: Path):
    jobs, store, _ = _jobs(tmp_path, object_store=FakeObjectStore())  # no prior store-backup run
    await jobs.run_integrity_drill()
    run = store.runs["run-1"]
    assert run.status == "failed" and "store bundle" in (run.error or "")


async def test_store_sweep_commits_and_records_no_agent_run(tmp_path: Path):
    git = FakeGitRepo()
    jobs, store, git = _jobs(tmp_path, object_store=None, git=git)
    await jobs.run_store_sweep()
    assert git.commits  # backup_now committed
    assert store.runs == {}  # the sweep is the git commit, not one of the four named jobs
