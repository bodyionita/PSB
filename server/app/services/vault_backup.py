"""Vault backup & history durability (ADR-014).

The capture/agent pipelines must not know *how* the vault is committed and pushed — only that
after a write batch they should request a backup. :class:`VaultBackup` is that seam;
:class:`VaultBackupService` is the real implementation.

Durability guarantees implemented here (ADR-014 §2–3):
  * **Always commit.** Debounced (~60s) batch commits coalesce a burst of writes into one
    commit; nothing is left uncommitted for long.
  * **One lock.** File writes stay concurrent + atomic (the ``NoteWriter``); *all* git
    staging/commit/push serialises behind a single :class:`asyncio.Lock` so batches never
    interleave.
  * **Fast-forward-only push, heal-on-reject.** Push is ordinary (ff-only); a non-fast-forward
    rejection is healed by a **merge** (never rebase/force/reset) then re-push — which also
    heals GitHub if a client ever rewound it. An unreachable remote is best-effort: commits
    stay local and the next backup reconciles (never block a write on the network, §5).
  * **gc/reflog pins + empty-repo bootstrap** are applied idempotently at startup.

The R2 WORM bundle, integrity drill, and the scheduler that drives the nightly jobs land in the
durability *scheduler* task; this module owns the git side.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Settings
from .git_repo import GitClient

logger = logging.getLogger(__name__)

# gc/reflog pins (ADR-014 §2): make history unprunable and disable background gc so a rewrite
# can never make old commits unreachable-then-collected. Idempotent — safe to set every boot.
_DURABILITY_CONFIG: tuple[tuple[str, str], ...] = (
    ("gc.pruneExpire", "never"),
    ("gc.reflogExpire", "never"),
    ("gc.reflogExpireUnreachable", "never"),
    ("gc.auto", "0"),
)

# ADR-014 §3: the vault .gitignore excludes ONLY Obsidian UI cache + OS cruft — never notes,
# never .trash (soft-deleted notes stay committed and backed up).
_GITIGNORE = """\
# ADR-014 §3 — exclude only Obsidian UI cache + OS cruft. Never notes, never .trash.
.obsidian/workspace*
.DS_Store
Thumbs.db
"""

_MAX_MESSAGE_CHARS = 500


class VaultBackup(Protocol):
    """What the capture/agent pipelines need from the backup layer (fire-and-forget)."""

    async def request_commit(self, reason: str) -> None:
        """Ask the backup layer to (eventually) commit + push the current vault state.

        Debounced and serialised; callers treat it as fire-and-forget and must not depend on
        completion for their success (notes are already on disk)."""
        ...


@dataclass(frozen=True)
class BackupResult:
    committed: bool
    pushed: bool


class VaultBackupService:
    """Owns every git operation on the vault, behind one lock (ADR-014)."""

    def __init__(self, *, settings: Settings, git: GitClient) -> None:
        self._settings = settings
        self._git = git
        self._remote = settings.vault_git_remote
        self._branch = settings.vault_git_branch
        self._debounce = settings.vault_backup_debounce_seconds
        self._lock = asyncio.Lock()
        self._pending: list[str] = []
        self._timer: asyncio.Task | None = None
        self._closing = False

    # --- startup -----------------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Idempotent boot step: init if needed, pin gc/reflog + identity, bootstrap if empty."""
        if not await self._git.is_repo():
            await self._git.init(self._branch)
        await self._apply_config()
        if not await self._git.has_head():
            await self._bootstrap_skeleton()

    async def _apply_config(self) -> None:
        for key, value in _DURABILITY_CONFIG:
            await self._git.set_config(key, value)
        # Commit identity so `git commit` works inside the container (no global identity there).
        await self._git.set_config("user.name", self._settings.git_user_name)
        await self._git.set_config("user.email", self._settings.git_user_email)

    async def _bootstrap_skeleton(self) -> None:
        """Empty repo → create the plane/summary folder skeleton + .gitignore, commit, push -u."""
        await asyncio.to_thread(
            _write_skeleton,
            self._settings.vault_path,
            list(self._settings.planes),
            self._settings.inbox_plane,
        )
        async with self._lock:
            await self._git.add_all()
            if await self._git.has_staged_changes():
                await self._git.commit("bootstrap: vault skeleton")
            if await self._git.has_remote(self._remote):
                await self._git.push(self._remote, self._branch, set_upstream=True)

    # --- public backup API -------------------------------------------------------------------

    async def request_commit(self, reason: str) -> None:
        """Fire-and-forget: enqueue a reason and (re)arm the debounce window."""
        self._pending.append(reason)
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._debounce_and_commit())

    async def backup_now(self, reason: str = "manual backup") -> BackupResult:
        """Immediate commit + push (POST /admin/backup + the 04:55 sweep). Folds in pending."""
        await self._cancel_timer()
        reasons = self._drain()
        reasons.append(reason)
        return await self._commit_and_push(reasons)

    async def flush(self) -> BackupResult:
        """Shutdown: stop the timer, commit any pending, and flush unpushed commits."""
        self._closing = True
        await self._cancel_timer()
        return await self._commit_and_push(self._drain())

    # --- internals ---------------------------------------------------------------------------

    async def _debounce_and_commit(self) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return  # cancelled mid-window — flush()/backup_now() will drain _pending
        reasons = self._drain()
        ok = True
        try:
            if reasons:
                # Shield the commit: a concurrent flush()/backup_now() may cancel this timer, but
                # an in-flight git batch must run to completion holding the lock — otherwise the
                # canceller would acquire the lock and run a second git command in parallel while
                # the (uncancellable) subprocess is still executing.
                await asyncio.shield(self._commit_and_push(reasons))
        except Exception:  # noqa: BLE001 — a backup must never crash the loop (rule 7)
            ok = False
            logger.exception("debounced vault backup failed (will retry)")
        # Re-arm if a write arrived while we were committing, or to retry a failed batch.
        if (self._pending or not ok) and not self._closing:
            self._timer = asyncio.create_task(self._debounce_and_commit())

    async def _commit_and_push(self, reasons: list[str]) -> BackupResult:
        async with self._lock:
            # Defensive: never commit on top of an in-progress merge (would capture conflict
            # markers as note content). Shouldn't happen — heal-on-reject aborts its own merges.
            if await self._git.is_merging():
                logger.error("in-progress merge before backup; aborting it to avoid markers")
                await self._git.abort_merge()
            await self._git.add_all()
            committed = False
            if await self._git.has_staged_changes():
                await self._git.commit(self._message(reasons))
                committed = True
            pushed = await self._push_with_heal()
            return BackupResult(committed=committed, pushed=pushed)

    async def _push_with_heal(self) -> bool:
        if not await self._git.has_remote(self._remote):
            return False
        outcome = await self._git.push(self._remote, self._branch)
        if outcome.ok:
            return True
        if outcome.non_fast_forward:
            logger.warning("vault push rejected (non-ff) — healing via merge, then retry")
            if await self._git.pull_merge(self._remote, self._branch):
                retry = await self._git.push(self._remote, self._branch)
                return retry.ok
            if not await self._git.abort_merge():
                logger.error("heal-merge abort failed; vault tree may be left mid-merge")
            logger.error("vault heal-merge failed; commits kept local for the next backup")
            return False
        logger.warning("vault push failed (remote unreachable?); commits kept local, will retry")
        return False

    async def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            try:
                await self._timer
            except asyncio.CancelledError:
                pass
            self._timer = None

    def _drain(self) -> list[str]:
        reasons = self._pending[:]
        self._pending.clear()
        return reasons

    def _message(self, reasons: list[str]) -> str:
        seen: list[str] = []
        for reason in reasons:
            if reason and reason not in seen:
                seen.append(reason)
        text = "; ".join(seen) if seen else "vault backup"
        return text[:_MAX_MESSAGE_CHARS]


def _write_skeleton(vault_path: str, planes: list[str], inbox_plane: str) -> None:
    """Create the vault folder skeleton with .gitkeep placeholders + the ADR-014 .gitignore."""
    root = Path(vault_path)
    root.mkdir(parents=True, exist_ok=True)
    folders = [inbox_plane, "Summaries/Daily", "Summaries/Weekly", *planes]
    for folder in folders:
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / ".gitkeep").touch()
    (root / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
