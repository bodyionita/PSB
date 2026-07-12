"""VaultBackupService orchestration tests — FakeGitRepo, no real git, deterministic.

The debounce timer is exercised by awaiting the service's internal timer task (no sleeps); the
coalescing/fold-in paths use a long debounce + flush()/backup_now() so nothing races.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Settings
from app.services.vault_backup import VaultBackupService

from .fakes import FakeGitRepo


def _service(tmp_path: Path, git: FakeGitRepo | None = None, *, debounce: float = 100.0):
    settings = Settings(
        vault_path=str(tmp_path / "vault"),
        planes=["Professional", "Ideas"],
        vault_backup_debounce_seconds=debounce,
    )
    git = git or FakeGitRepo()
    return VaultBackupService(settings=settings, git=git), git


async def test_ensure_ready_bootstraps_empty_repo(tmp_path: Path):
    git = FakeGitRepo(is_repo=False, has_head=False)
    service, git = _service(tmp_path, git)
    await service.ensure_ready()

    assert git.inited is True
    # gc/reflog pins + commit identity applied (ADR-014 §2).
    assert git.config["gc.pruneExpire"] == "never"
    assert git.config["gc.reflogExpireUnreachable"] == "never"
    assert git.config["user.name"] == "Braindan"
    # Skeleton committed + pushed with upstream.
    assert git.commits == ["bootstrap: vault skeleton"]
    assert git.pushes == 1
    # Folder skeleton on disk: inbox, summaries, each plane, + .gitignore (ADR-014 §3).
    vault = tmp_path / "vault"
    assert (vault / "Inbox" / ".gitkeep").exists()
    assert (vault / "Summaries" / "Daily" / ".gitkeep").exists()
    assert (vault / "Ideas" / ".gitkeep").exists()
    gitignore = (vault / ".gitignore").read_text(encoding="utf-8")
    assert ".obsidian/workspace*" in gitignore
    assert ".idea/" in gitignore  # JetBrains cruft ignored (added M1)
    # .gitattributes ships in the bootstrap commit so notes are LF everywhere (no CRLF churn).
    assert "*.md text eol=lf" in (vault / ".gitattributes").read_text(encoding="utf-8")
    # No active ignore pattern touches .trash — soft-deleted notes stay tracked (ADR-014 §3).
    ignore_lines = [ln for ln in gitignore.splitlines() if ln and not ln.startswith("#")]
    assert not any(".trash" in ln for ln in ignore_lines)


async def test_ensure_ready_existing_repo_adds_missing_housekeeping(tmp_path: Path):
    # An existing vault that predates the housekeeping files gets them added (one commit), but is
    # NOT re-bootstrapped.
    git = FakeGitRepo(is_repo=True, has_head=True)
    service, git = _service(tmp_path, git)
    await service.ensure_ready()

    assert git.inited is False
    assert git.commits == ["housekeeping: .gitignore + .gitattributes"]  # no bootstrap commit
    assert git.config["gc.auto"] == "0"  # config still pinned
    assert (tmp_path / "vault" / ".gitattributes").exists()


async def test_ensure_ready_housekeeping_idempotent_when_current(tmp_path: Path):
    # If the housekeeping files already match, ensure_ready makes no commit (true idempotency).
    from app.services.vault_backup import _GITATTRIBUTES, _GITIGNORE

    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    (vault / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (vault / ".gitattributes").write_text(_GITATTRIBUTES, encoding="utf-8")
    git = FakeGitRepo(is_repo=True, has_head=True)
    service, git = _service(tmp_path, git)
    await service.ensure_ready()

    assert git.commits == []


async def test_debounced_commit_fires_and_pushes(tmp_path: Path):
    service, git = _service(tmp_path, debounce=0.01)
    await service.request_commit("capture abc")
    await service._timer  # deterministic: wait for the armed window to complete

    assert git.commits == ["capture abc"]
    assert git.pushes == 1


async def test_requests_coalesce_into_one_commit(tmp_path: Path):
    # Long debounce so the timer never fires; flush() commits the whole batch at once.
    service, git = _service(tmp_path, debounce=100.0)
    await service.request_commit("capture x")
    await service.request_commit("capture x")  # duplicate reason deduped in the message
    await service.request_commit("capture y")
    result = await service.flush()

    assert result.committed is True
    assert git.commits == ["capture x; capture y"]
    assert git.pushes == 1


async def test_push_heals_on_non_fast_forward(tmp_path: Path):
    git = FakeGitRepo()
    git.non_ff_times = 1  # first push rejected non-ff, then the retry succeeds
    service, git = _service(tmp_path, git)
    result = await service.backup_now("manual")

    assert result.committed is True and result.pushed is True
    # 1 proactive pull (pull-first) + 1 heal pull on the non-ff rejection; then the retry pushes.
    assert git.pushes == 2 and git.pulls == 2 and git.aborts == 0


async def test_push_heal_merge_failure_aborts_and_keeps_local(tmp_path: Path):
    git = FakeGitRepo()
    git.non_ff_times = 1
    git.pull_ok = False  # heal-merge fails
    service, git = _service(tmp_path, git)
    result = await service.backup_now("manual")

    assert result.committed is True  # commit still landed locally
    assert result.pushed is False
    assert git.aborts == 1  # merge aborted to leave a clean tree


async def test_no_remote_commits_locally(tmp_path: Path):
    git = FakeGitRepo(has_remote=False)
    service, git = _service(tmp_path, git)
    result = await service.backup_now("manual")

    assert result.committed is True and result.pushed is False
    assert git.pushes == 0


async def test_no_changes_still_flushes_unpushed(tmp_path: Path):
    git = FakeGitRepo()
    git.staged_after_add = False  # nothing new to commit
    service, git = _service(tmp_path, git)
    result = await service.backup_now("manual")

    assert result.committed is False
    assert result.pushed is True  # push still attempted (flushes prior unpushed commits)
    assert git.commits == []


async def test_backup_now_folds_in_pending(tmp_path: Path):
    service, git = _service(tmp_path, debounce=100.0)
    await service.request_commit("capture a")
    await service.request_commit("capture b")
    result = await service.backup_now("manual backup")

    assert result.committed is True
    assert git.commits == ["capture a; capture b; manual backup"]
    assert service._timer is None  # pending timer cancelled


async def test_inflight_commit_not_abandoned_by_concurrent_cancel(tmp_path: Path):
    # Regression: a debounce commit that is mid-flight when flush() cancels the timer must run to
    # completion (shielded), so the canceller can't run a second git batch in parallel. Without
    # the shield the in-flight commit is cancelled and "capture a" is lost.
    git = FakeGitRepo()
    git.commit_entered = asyncio.Event()
    git.commit_gate = asyncio.Event()
    service, git = _service(tmp_path, git, debounce=0.01)

    await service.request_commit("capture a")
    await git.commit_entered.wait()  # timer fired; inside commit, blocked on the gate
    flush_task = asyncio.create_task(service.flush())  # cancels the timer mid-commit
    await asyncio.sleep(0)  # let flush cancel the timer and block on the lock
    git.commit_gate.set()  # release the in-flight commit
    await flush_task

    assert git.commits and git.commits[0] == "capture a"  # not abandoned by the cancel


async def test_flush_with_nothing_pending_is_safe(tmp_path: Path):
    git = FakeGitRepo()
    git.staged_after_add = False
    service, git = _service(tmp_path, git)
    result = await service.flush()

    assert result.committed is False
    assert git.commits == []
