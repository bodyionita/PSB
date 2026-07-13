"""GitRepo + StoreBackupService against REAL git (bare remote, no network).

Validates the concrete wrapper and the end-to-end durability path (bootstrap → push, and a real
non-fast-forward heal-on-reject merge). Skipped when git isn't installed so CI stays green.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.services.backup_jobs import BackupJobs
from app.services.git_repo import GitRepo
from app.services.store_backup import StoreBackupService

from .fakes import FakeAgentRunStore, FakeObjectStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _clone(remote: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", str(remote), str(dest)], check=True, capture_output=True, text=True
    )


def _init_work_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True
    )
    # Point the bare HEAD at main so a later clone checks the branch out (bare defaults vary).
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True, text=True)
    _git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(work, "remote", "add", "origin", str(remote))
    return work, remote


def _settings(work: Path) -> Settings:
    return Settings(graph_store_path=str(work), store_backup_debounce_seconds=100.0)


async def test_repo_lifecycle_primitives(tmp_path: Path):
    repo = GitRepo(str(tmp_path / "r"))
    assert await repo.is_repo() is False
    await repo.init("main")
    assert await repo.is_repo() is True
    assert await repo.has_head() is False

    await repo.set_config("user.name", "Test")
    await repo.set_config("user.email", "test@example.com")
    (tmp_path / "r" / "a.md").write_text("hi", encoding="utf-8")

    assert await repo.has_staged_changes() is False
    await repo.add_all()
    assert await repo.has_staged_changes() is True
    await repo.commit("first")
    assert await repo.has_head() is True
    assert await repo.head_sha() is not None
    assert await repo.has_staged_changes() is False


async def test_bootstrap_and_backup_reach_remote(tmp_path: Path):
    work, remote = _init_work_with_remote(tmp_path)
    service = StoreBackupService(settings=_settings(work), git=GitRepo(str(work)))
    await service.ensure_ready()  # bootstrap → commit + push -u

    clone = tmp_path / "clone"
    _clone(remote, clone)
    assert (clone / "memory" / ".gitkeep").exists()  # a node-type folder in the skeleton
    assert (clone / "inbox" / ".gitkeep").exists()
    assert (clone / ".gitignore").exists()

    (work / "memory" / "node.md").write_text("content", encoding="utf-8")
    result = await service.backup_now("capture 1")
    assert result.committed is True and result.pushed is True

    _git(clone, "pull")
    assert (clone / "memory" / "node.md").exists()


async def test_backup_heals_real_non_fast_forward(tmp_path: Path):
    work, remote = _init_work_with_remote(tmp_path)
    service = StoreBackupService(settings=_settings(work), git=GitRepo(str(work)))
    await service.ensure_ready()

    # Another clone pushes a divergent commit, so the server's next push is rejected non-ff.
    other = tmp_path / "other"
    _clone(remote, other)
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.com")
    (other / "memory" / "remote.md").write_text("from other", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "other change")
    _git(other, "push", "origin", "HEAD:main")

    # Server writes locally, then backup → push rejected → heal-merge → re-push succeeds.
    (work / "memory" / "local.md").write_text("from local", encoding="utf-8")
    result = await service.backup_now("capture local")
    assert result.committed is True and result.pushed is True

    _git(other, "pull")
    assert (other / "memory" / "remote.md").exists()
    assert (other / "memory" / "local.md").exists()  # both sides preserved by the merge


async def test_store_bundle_and_drill_roundtrip(tmp_path: Path):
    # End-to-end with REAL git: bundle the live store → R2 (fake store), then the drill downloads,
    # verifies, clones, and asserts the fingerprint + monotonic count against the live repo.
    work, _ = _init_work_with_remote(tmp_path)
    settings = Settings(
        graph_store_path=str(work), data_path=str(tmp_path / "data"), scheduler_tz="UTC"
    )
    store_backup = StoreBackupService(settings=settings, git=GitRepo(str(work)))
    await store_backup.ensure_ready()  # bootstrap commit → a real history to bundle

    obj, store = FakeObjectStore(), FakeAgentRunStore()
    jobs = BackupJobs(settings=settings, store=store, object_store=obj, store_backup=store_backup)

    await jobs.run_store_bundle()
    assert store.runs["run-1"].status == "succeeded", store.runs["run-1"].error
    assert any(k.endswith(".bundle") for k in obj.objects)

    # Uses the real default bundle_inspector (git bundle verify + clone) against the fake R2.
    await jobs.run_integrity_drill()
    drill = store.runs["run-2"]
    assert drill.status == "succeeded", drill.error
