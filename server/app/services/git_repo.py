"""Thin git wrapper for the vault repo (ADR-014).

All git in the app goes through here so :class:`VaultBackupService` can compose the durability
guarantees (always commit, **fast-forward-only** push, **heal-on-reject** merge — never
force/rebase/reset) on top of a testable seam. Subprocess calls block, so each runs in a worker
thread (CLAUDE.md rule 8). This wrapper issues single git commands only; the *service* — never
this wrapper — owns the one lock that serialises staging/commit/push.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PushOutcome:
    """Result of a push attempt. ``non_fast_forward`` distinguishes a rewind-heal case (pull+
    retry) from a plain failure (unreachable remote → best-effort, retry next backup)."""

    ok: bool
    non_fast_forward: bool = False


class GitError(RuntimeError):
    """A git command that was required to succeed exited non-zero."""


class GitClient(Protocol):
    """The git surface :class:`VaultBackupService` depends on (fakeable in tests)."""

    async def is_repo(self) -> bool: ...
    async def has_head(self) -> bool: ...
    async def init(self, branch: str) -> None: ...
    async def set_config(self, key: str, value: str) -> None: ...
    async def has_remote(self, name: str) -> bool: ...
    async def add_all(self) -> None: ...
    async def has_staged_changes(self) -> bool: ...
    async def commit(self, message: str) -> None: ...
    async def push(
        self, remote: str, branch: str, *, set_upstream: bool = False
    ) -> PushOutcome: ...
    async def pull_merge(self, remote: str, branch: str) -> bool: ...
    async def is_merging(self) -> bool: ...
    async def abort_merge(self) -> bool: ...
    async def head_sha(self) -> str | None: ...
    async def commit_count(self) -> int: ...
    async def tracked_file_count(self) -> int: ...
    async def bundle_all(self, path: str) -> None: ...


class GitRepo:
    """asyncpg-style concrete wrapper: every method shells one git command in a worker thread."""

    def __init__(self, path: str, *, timeout: float = 120.0) -> None:
        self._path = Path(path)
        self._timeout = timeout

    def _run_sync(self, *args: str, check: bool) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(self._path), *args],
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    async def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return await asyncio.to_thread(self._run_sync, *args, check=check)

    async def is_repo(self) -> bool:
        def _check() -> bool:
            if not self._path.exists():
                return False
            r = subprocess.run(
                ["git", "-C", str(self._path), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            return r.returncode == 0 and r.stdout.strip() == "true"

        return await asyncio.to_thread(_check)

    async def has_head(self) -> bool:
        r = await self._run("rev-parse", "--verify", "HEAD", check=False)
        return r.returncode == 0

    async def init(self, branch: str) -> None:
        def _init() -> None:
            self._path.mkdir(parents=True, exist_ok=True)
            base = ["git", "-C", str(self._path)]
            subprocess.run([*base, "init"], capture_output=True, text=True, timeout=self._timeout,
                           check=True)
            # Name the branch deterministically, independent of the host's init.defaultBranch.
            subprocess.run([*base, "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
                           capture_output=True, text=True, timeout=self._timeout, check=True)

        await asyncio.to_thread(_init)

    async def set_config(self, key: str, value: str) -> None:
        await self._run("config", key, value)

    async def has_remote(self, name: str) -> bool:
        r = await self._run("remote", check=False)
        return name in r.stdout.split()

    async def add_all(self) -> None:
        await self._run("add", "-A")

    async def has_staged_changes(self) -> bool:
        # `diff --cached --quiet` exits 1 when there is something staged, 0 when clean.
        r = await self._run("diff", "--cached", "--quiet", check=False)
        return r.returncode != 0

    async def commit(self, message: str) -> None:
        await self._run("commit", "-m", message)

    async def push(self, remote: str, branch: str, *, set_upstream: bool = False) -> PushOutcome:
        args = ["push", *(["-u"] if set_upstream else []), remote, f"HEAD:{branch}"]
        try:
            r = await self._run(*args, check=False)
        except subprocess.TimeoutExpired:
            # A hung/slow remote is a soft failure — push is best-effort (ADR-014 §5), never
            # block or crash a write on the network. Commits stay local; the next backup retries.
            logger.warning("git push timed out; treating as a soft failure (commits kept local)")
            return PushOutcome(ok=False)
        if r.returncode == 0:
            return PushOutcome(ok=True)
        stderr = r.stderr.lower()
        non_ff = any(s in stderr for s in ("non-fast-forward", "fetch first", "[rejected]"))
        return PushOutcome(ok=False, non_fast_forward=non_ff)

    async def pull_merge(self, remote: str, branch: str) -> bool:
        # Merge, never rebase (ADR-014 §4). --no-edit keeps a clean merge non-interactive.
        try:
            r = await self._run("pull", "--no-rebase", "--no-edit", remote, branch, check=False)
        except subprocess.TimeoutExpired:
            logger.warning("git pull (heal-merge) timed out")
            return False
        return r.returncode == 0

    async def is_merging(self) -> bool:
        """True if a merge is in progress (MERGE_HEAD exists) — tree may hold conflict markers."""
        r = await self._run("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
        return r.returncode == 0

    async def abort_merge(self) -> bool:
        # Best-effort: restore a clean tree after a conflicted heal-merge so later commits aren't
        # made mid-MERGING. Never raises; returns whether the abort succeeded.
        r = await self._run("merge", "--abort", check=False)
        return r.returncode == 0

    async def head_sha(self) -> str | None:
        r = await self._run("rev-parse", "HEAD", check=False)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    async def commit_count(self) -> int:
        # Total commits across all refs — a monotonic non-decreasing durability fingerprint (§6).
        r = await self._run("rev-list", "--all", "--count", check=False)
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0

    async def tracked_file_count(self) -> int:
        r = await self._run("ls-files", check=False)
        return sum(1 for line in r.stdout.splitlines() if line.strip()) if r.returncode == 0 else 0

    async def bundle_all(self, path: str) -> None:
        # A self-contained, `git bundle verify`-able snapshot of the ENTIRE history (ADR-014 §1).
        await self._run("bundle", "create", path, "--all")

    async def verify_bundle(self, path: str) -> bool:
        r = await self._run("bundle", "verify", path, check=False)
        return r.returncode == 0

    @staticmethod
    async def clone_from(source: str, dest: str) -> None:
        await asyncio.to_thread(
            lambda: subprocess.run(
                ["git", "clone", source, dest],
                capture_output=True, text=True, timeout=120.0, check=True,
            )
        )
