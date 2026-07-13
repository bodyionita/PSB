"""Graph-store backup & history durability (ADR-014; ex-``vault_backup``, renamed at M3).

The capture/agent pipelines must not know *how* the store is committed and pushed — only that
after a write batch they should request a backup. :class:`StoreBackup` is that seam;
:class:`StoreBackupService` is the real implementation. ADR-014 machinery is unchanged by the
pivot — only the vocabulary (vault → graph store) and the bootstrap skeleton (plane/summary
folders → node-type folders + ``inbox/``) moved.

Durability guarantees implemented here (ADR-014 §2–3):
  * **Always commit.** Debounced (~60s) batch commits coalesce a burst of writes into one
    commit; nothing is left uncommitted for long.
  * **One lock.** File writes stay concurrent + atomic (the ``NodeWriter``); *all* git
    staging/commit/push serialises behind a single :class:`asyncio.Lock` so batches never
    interleave.
  * **Fast-forward-only push, heal-on-reject.** Push is ordinary (ff-only); a non-fast-forward
    rejection is healed by a **merge** (never rebase/force/reset) then re-push — which also
    heals GitHub if a client ever rewound it. An unreachable remote is best-effort: commits
    stay local and the next backup reconciles (never block a write on the network, §5).
  * **gc/reflog pins + empty-repo bootstrap** are applied idempotently at startup. The bootstrap
    also wires the ``GRAPH_STORE_REPO`` remote (ADR-031 §6 — zero manual VPS steps).

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

# ADR-014 §3 / ADR-031 §6: the store .gitignore excludes ONLY editor/OS cruft — never nodes,
# never .trash (soft-deleted nodes stay committed and backed up). Obsidian is gone (ADR-026), so
# no `.obsidian` entry.
_GITIGNORE = """\
# ADR-014 §3 / ADR-031 — exclude only editor/OS cruft. Never nodes, never .trash.
.idea/
.DS_Store
Thumbs.db
"""

# Normalise line endings so editing a node on any OS doesn't surface spurious whole-file diffs.
# Nodes are always stored LF in the repo.
_GITATTRIBUTES = """\
* text=auto eol=lf
*.md text eol=lf
"""

_MAX_MESSAGE_CHARS = 500


class StoreBackup(Protocol):
    """What the capture/agent pipelines need from the backup layer (fire-and-forget)."""

    async def request_commit(self, reason: str) -> None:
        """Ask the backup layer to (eventually) commit + push the current store state.

        Debounced and serialised; callers treat it as fire-and-forget and must not depend on
        completion for their success (nodes are already on disk)."""
        ...


@dataclass(frozen=True)
class BackupResult:
    committed: bool
    pushed: bool


class StoreCommitter(Protocol):
    """A forced commit+push of the current store state — the narrow view the reindex / tag-apply /
    merge / backfill jobs need of the backup layer (they rewrite files then checkpoint)."""

    async def backup_now(self, reason: str = ...) -> BackupResult: ...


@dataclass(frozen=True)
class Fingerprint:
    """Store durability fingerprint (ADR-014 §6): HEAD sha + monotonic commit count + file count."""

    head_sha: str | None
    commit_count: int
    file_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "head_sha": self.head_sha,
            "commit_count": self.commit_count,
            "file_count": self.file_count,
        }


class StoreBackupService:
    """Owns every git operation on the graph store, behind one lock (ADR-014)."""

    def __init__(self, *, settings: Settings, git: GitClient) -> None:
        self._settings = settings
        self._git = git
        self._remote = settings.store_git_remote
        self._branch = settings.store_git_branch
        self._debounce = settings.store_backup_debounce_seconds
        self._lock = asyncio.Lock()
        self._pending: list[str] = []
        self._timer: asyncio.Task | None = None
        self._closing = False

    # --- startup -----------------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Idempotent boot step: init if needed, wire the GRAPH_STORE_REPO remote, pin gc/reflog +
        identity, bootstrap the node-type skeleton if empty, and reconcile the housekeeping files
        (.gitignore/.gitattributes) with the current repo."""
        if not await self._git.is_repo():
            await self._git.init(self._branch)
        if self._settings.graph_store_repo:
            await self._git.set_remote(self._remote, self._settings.graph_store_repo)
        await self._apply_config()
        if not await self._git.has_head():
            await self._bootstrap_skeleton()
        await self._ensure_housekeeping()

    async def _ensure_housekeeping(self) -> None:
        """Keep the store's .gitignore + .gitattributes matching the canonical content on every
        boot (an existing store predating a change gets updated), committing + pushing only if
        they actually changed. Idempotent — a no-op once the repo is current."""
        changed = await asyncio.to_thread(_write_housekeeping, self._settings.graph_store_path)
        if changed:
            await self._commit_and_push(["housekeeping: .gitignore + .gitattributes"])

    async def _apply_config(self) -> None:
        for key, value in _DURABILITY_CONFIG:
            await self._git.set_config(key, value)
        # Commit identity so `git commit` works inside the container (no global identity there).
        await self._git.set_config("user.name", self._settings.git_user_name)
        await self._git.set_config("user.email", self._settings.git_user_email)

    async def _bootstrap_skeleton(self) -> None:
        """Empty repo → create the node-type folder skeleton + housekeeping, commit, push -u."""
        await asyncio.to_thread(
            _write_skeleton,
            self._settings.graph_store_path,
            list(self._settings.node_types),
            self._settings.inbox_folder,
        )
        async with self._lock:
            await self._git.add_all()
            if await self._git.has_staged_changes():
                await self._git.commit("bootstrap: graph-store skeleton")
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

    async def sync_from_remote(self) -> None:
        """Pull remote edits into the working tree before a full rescan (the nightly reindex,
        04-pipelines §4). Commits any pending local writes first so the merge has a clean tree,
        then merge-pulls — all under the one lock, so it never races a concurrent commit.
        Best-effort (§5): an unreachable remote or a conflicting merge is cleaned up and the
        rescan simply runs on the current on-disk state; the local store is never lost.

        The debounce timer is cancelled and its pending reasons drained up-front (as ``backup_now``
        does) so the day's queued writes land in *this* commit with their own reasons — the later
        ``backup_now`` then carries only the reindex's changes, keeping the 'one commit+push for
        the reindex output' story clean (no stale reason on a redundant push)."""
        await self._cancel_timer()
        reasons = self._drain()
        async with self._lock:
            # Never pull on top of an in-progress merge (would fold conflict markers into nodes).
            if await self._git.is_merging():
                logger.error("in-progress merge before reindex pull; aborting it")
                await self._git.abort_merge()
            await self._git.add_all()
            if await self._git.has_staged_changes():
                reasons.append("reindex: commit pending store writes before pull")
                await self._git.commit(self._message(reasons))
            await self._integrate_remote()

    async def flush(self) -> BackupResult:
        """Shutdown: stop the timer, commit any pending, and flush unpushed commits."""
        self._closing = True
        await self._cancel_timer()
        return await self._commit_and_push(self._drain())

    # --- durability snapshots (used by the nightly R2 bundle job) ----------------------------

    async def snapshot_fingerprint(self) -> Fingerprint:
        """Fingerprint the live repo (under the lock, so it never races a commit)."""
        async with self._lock:
            return await self._fingerprint()

    async def write_bundle(self, path: str) -> Fingerprint:
        """Write a full-history `git bundle` and return its fingerprint (both under the lock)."""
        async with self._lock:
            await self._git.bundle_all(path)
            return await self._fingerprint()

    async def _fingerprint(self) -> Fingerprint:
        return Fingerprint(
            head_sha=await self._git.head_sha(),
            commit_count=await self._git.commit_count(),
            file_count=await self._git.tracked_file_count(),
        )

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
            logger.exception("debounced store backup failed (will retry)")
        # Re-arm if a write arrived while we were committing, or to retry a failed batch.
        if (self._pending or not ok) and not self._closing:
            self._timer = asyncio.create_task(self._debounce_and_commit())

    async def _commit_and_push(self, reasons: list[str]) -> BackupResult:
        async with self._lock:
            # Defensive: never commit on top of an in-progress merge (would capture conflict
            # markers as node content). Shouldn't happen — heal-on-reject aborts its own merges.
            if await self._git.is_merging():
                logger.error("in-progress merge before backup; aborting it to avoid markers")
                await self._git.abort_merge()
            await self._git.add_all()
            committed = False
            if await self._git.has_staged_changes():
                await self._git.commit(self._message(reasons))
                committed = True
            # Pull-first (ADR-014 amendment): integrate remote edits (made on GitHub or another
            # device) before pushing, so the push is a plain fast-forward and the local store
            # stays current. Done AFTER committing local work so the tree is clean for the merge.
            await self._integrate_remote()
            pushed = await self._push_with_heal()
            return BackupResult(committed=committed, pushed=pushed)

    async def _integrate_remote(self) -> None:
        """Best-effort merge-pull of the remote before pushing. An unreachable remote or a
        conflicting merge must never lose the local commit (§5): clean up any half-merge and
        carry on — ``_push_with_heal`` still tries, and the next backup reconciles."""
        if not await self._git.has_remote(self._remote):
            return
        if not await self._git.pull_merge(self._remote, self._branch):
            if await self._git.is_merging():
                await self._git.abort_merge()

    async def _push_with_heal(self) -> bool:
        if not await self._git.has_remote(self._remote):
            return False
        outcome = await self._git.push(self._remote, self._branch)
        if outcome.ok:
            return True
        if outcome.non_fast_forward:
            logger.warning("store push rejected (non-ff) — healing via merge, then retry")
            if await self._git.pull_merge(self._remote, self._branch):
                retry = await self._git.push(self._remote, self._branch)
                return retry.ok
            if not await self._git.abort_merge():
                logger.error("heal-merge abort failed; store tree may be left mid-merge")
            logger.error("store heal-merge failed; commits kept local for the next backup")
            return False
        logger.warning("store push failed (remote unreachable?); commits kept local, will retry")
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
        text = "; ".join(seen) if seen else "store backup"
        return text[:_MAX_MESSAGE_CHARS]


def _write_skeleton(graph_store_path: str, node_types: list[str], inbox_folder: str) -> None:
    """Create the node-type folder skeleton with .gitkeep placeholders + housekeeping (02 §1)."""
    root = Path(graph_store_path)
    root.mkdir(parents=True, exist_ok=True)
    for folder in [*node_types, inbox_folder]:
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / ".gitkeep").touch()
    # Housekeeping files in the bootstrap commit so a fresh store needs no follow-up commit.
    _write_housekeeping(graph_store_path)


def _write_housekeeping(graph_store_path: str) -> bool:
    """Ensure .gitignore + .gitattributes match the canonical content. Writes only files whose
    content differs; returns True if any changed (so the caller commits). Newline-exact so an
    unchanged repo is a genuine no-op."""
    root = Path(graph_store_path)
    root.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, content in ((".gitignore", _GITIGNORE), (".gitattributes", _GITATTRIBUTES)):
        path = root / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != content:
            path.write_text(content, encoding="utf-8")
            changed = True
    return changed
