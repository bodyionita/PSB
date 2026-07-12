"""GitRepo + VaultBackupService against REAL git (bare remote, no network).

Validates the concrete wrapper and the end-to-end durability path (bootstrap → push, and a real
non-fast-forward heal-on-reject merge). Skipped when git isn't installed so CI stays green.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.services.git_repo import GitRepo
from app.services.vault_backup import VaultBackupService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _clone(remote: Path, dest: Path) -> None:
    subprocess.run(["git", "clone", str(remote), str(dest)], check=True, capture_output=True,
                   text=True)


def _init_work_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True,
                   text=True)
    # Point the bare HEAD at main so a later clone checks the branch out (bare defaults vary).
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True, text=True)
    _git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(work, "remote", "add", "origin", str(remote))
    return work, remote


def _settings(work: Path) -> Settings:
    return Settings(
        vault_path=str(work), planes=["Ideas"], vault_backup_debounce_seconds=100.0
    )


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
    service = VaultBackupService(settings=_settings(work), git=GitRepo(str(work)))
    await service.ensure_ready()  # bootstrap → commit + push -u

    clone = tmp_path / "clone"
    _clone(remote, clone)
    assert (clone / "Ideas" / ".gitkeep").exists()
    assert (clone / ".gitignore").exists()

    (work / "Ideas" / "note.md").write_text("content", encoding="utf-8")
    result = await service.backup_now("capture 1")
    assert result.committed is True and result.pushed is True

    _git(clone, "pull")
    assert (clone / "Ideas" / "note.md").exists()


async def test_backup_heals_real_non_fast_forward(tmp_path: Path):
    work, remote = _init_work_with_remote(tmp_path)
    service = VaultBackupService(settings=_settings(work), git=GitRepo(str(work)))
    await service.ensure_ready()

    # Another clone pushes a divergent commit, so the server's next push is rejected non-ff.
    other = tmp_path / "other"
    _clone(remote, other)
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.com")
    (other / "Ideas" / "remote.md").write_text("from other", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "other change")
    _git(other, "push", "origin", "HEAD:main")

    # Server writes locally, then backup → push rejected → heal-merge → re-push succeeds.
    (work / "Ideas" / "local.md").write_text("from local", encoding="utf-8")
    result = await service.backup_now("capture local")
    assert result.committed is True and result.pushed is True

    _git(other, "pull")
    assert (other / "Ideas" / "remote.md").exists()
    assert (other / "Ideas" / "local.md").exists()  # both sides preserved by the merge
