"""Durability backup jobs (ADR-014 §1, §6, §7).

The four nightly/weekly R2 jobs plus the 04:55 vault sweep, each wrapped in an ``agent_runs`` row
(vision P8 "everything visible"; rule 7 "no bare except — jobs end as failed with context, never
crash the service"). All git ops on the *live* vault go through :class:`VaultBackupService` (the
one lock, ADR-014); this module never touches the live repo directly.

Jobs are pure orchestration over injected seams (``AgentRunStore``, ``ObjectStore``,
``VaultBackupService``, a db-dumper, a bundle-inspector), so they unit-test with fakes; the real
`git bundle` round-trip is integration-tested against actual git.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import Settings
from ..db import Database
from .agent_runs import FAILED, SKIPPED, SUCCEEDED, AgentRunStore, PgAgentRunStore
from .git_repo import GitRepo
from .object_store import ObjectStore, build_object_store
from .vault_backup import Fingerprint, VaultBackupService

logger = logging.getLogger(__name__)

# agent_runs.agent names (04-pipelines §5, ADR-014).
VAULT_BACKUP = "vault-backup"
INTEGRITY_DRILL = "integrity-drill"
DB_BACKUP = "db-backup"
DATA_SYNC = "data-sync"


class DurabilityError(RuntimeError):
    """A durability invariant was violated (fails the run → visible; drill → /health degrades)."""


class DrillError(DurabilityError):
    """The weekly integrity drill found a problem (fails the run → /health degrades)."""


DbDumper = Callable[[], Awaitable[bytes]]
BundleInspector = Callable[[bytes], Awaitable[Fingerprint]]


class BackupJobs:
    def __init__(
        self,
        *,
        settings: Settings,
        store: AgentRunStore,
        object_store: ObjectStore | None,
        vault_backup: VaultBackupService,
        db_dumper: DbDumper | None = None,
        bundle_inspector: BundleInspector | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._object_store = object_store
        self._vault_backup = vault_backup
        self._tz = ZoneInfo(settings.scheduler_tz)
        self._db_dumper = db_dumper or self._default_pg_dump
        self._bundle_inspector = bundle_inspector or self._default_bundle_inspector

    # --- jobs --------------------------------------------------------------------------------

    async def run_vault_bundle(self) -> None:
        """Nightly `git bundle --all` → R2 WORM, recording the fingerprint (ADR-014 §1, §6)."""
        await self._record_r2_job(VAULT_BACKUP, self._vault_bundle)

    async def run_integrity_drill(self) -> None:
        """Weekly: verify + clone the R2 bundle, assert fingerprint + monotonic count (§6)."""
        await self._record_r2_job(INTEGRITY_DRILL, self._integrity_drill)

    async def run_db_backup(self) -> None:
        """Nightly `pg_dump` → R2 — a second independent copy of operational state (§7)."""
        await self._record_r2_job(DB_BACKUP, self._db_backup)

    async def run_data_sync(self) -> None:
        """Nightly sync of raw inputs (DATA_PATH) → R2 (§7)."""
        await self._record_r2_job(DATA_SYNC, self._data_sync)

    async def run_vault_sweep(self) -> None:
        """04:55 sweep: force a vault commit + push so nothing sits uncommitted overnight."""
        result = await self._vault_backup.backup_now("nightly sweep")
        logger.info("nightly vault sweep: committed=%s pushed=%s", result.committed, result.pushed)

    # --- job bodies (return summary + details; raise to fail the run) -------------------------

    async def _vault_bundle(self) -> tuple[str, dict]:
        assert self._object_store is not None
        # The last GOOD bundle's count is the monotonic baseline (ADR-014 §6).
        previous = await self._store.latest(VAULT_BACKUP, status=SUCCEEDED)
        stamp = self._stamp()
        work_dir = Path(await _to_thread(tempfile.mkdtemp, prefix="vault-bundle-"))
        try:
            bundle_path = work_dir / f"vault-{stamp}.bundle"
            fingerprint = await self._vault_backup.write_bundle(str(bundle_path))
            data = await _to_thread(bundle_path.read_bytes)
            key = f"vault/bundle-{stamp}.bundle"
            # Upload to WORM first — even a regressed snapshot is worth keeping for forensics —
            # then fail the run if the count dropped, so this bundle does NOT become the new good
            # baseline and the weekly drill still compares live against the last good count.
            await self._object_store.put_bytes(key, data)
            manifest = {**fingerprint.as_dict(), "key": key, "bytes": len(data)}
            await self._object_store.put_bytes(
                f"{key}.manifest.json",
                json.dumps(manifest).encode("utf-8"),
                content_type="application/json",
            )
            if previous is not None:
                baseline = int(previous.details.get("commit_count", 0))
                if fingerprint.commit_count < baseline:
                    raise DurabilityError(
                        f"vault commit count regressed {baseline} → {fingerprint.commit_count} "
                        "(rewrite/truncation alarm, ADR-014 §6)"
                    )
            summary = f"bundled {fingerprint.commit_count} commits ({len(data)} bytes) → {key}"
            return summary, manifest
        finally:
            await _to_thread(shutil.rmtree, work_dir, True)

    async def _integrity_drill(self) -> tuple[str, dict]:
        assert self._object_store is not None
        # Drill the last KNOWN-GOOD bundle, so a failed nightly can't blind the weekly check.
        last = await self._store.latest(VAULT_BACKUP, status=SUCCEEDED)
        if last is None or not last.details.get("key"):
            raise DrillError("no successful vault bundle recorded yet to drill")
        manifest = last.details
        data = await self._object_store.get_bytes(str(manifest["key"]))
        cloned = await self._bundle_inspector(data)  # verifies + clones the bundle
        # The live server repo is a ff-only mirror of GitHub, so it stands in for the "and GitHub"
        # side of ADR-014 §6. An independent GitHub-side fetch is a tracked follow-up.
        live = await self._vault_backup.snapshot_fingerprint()

        problems: list[str] = []
        if cloned.commit_count != manifest.get("commit_count"):
            problems.append("bundle commit-count ≠ manifest")
        if cloned.head_sha != manifest.get("head_sha"):
            problems.append("bundle HEAD ≠ manifest")
        # Monotonic non-decreasing: the live repo must never have fewer commits than the last
        # bundle — a drop signals a rewrite/truncation (ADR-014 §6).
        if live.commit_count < int(manifest.get("commit_count", 0)):
            problems.append("live commit count regressed (rewrite/truncation alarm)")
        if problems:
            raise DrillError("; ".join(problems))

        details = {"bundle": manifest, "live": live.as_dict()}
        return f"integrity ok: {cloned.commit_count} commits verified from R2 bundle", details

    async def _db_backup(self) -> tuple[str, dict]:
        assert self._object_store is not None
        data = await self._db_dumper()
        key = f"db/pg_dump-{self._stamp()}.sql"
        await self._object_store.put_bytes(key, data, content_type="application/sql")
        return f"pg_dump {len(data)} bytes → {key}", {"key": key, "bytes": len(data)}

    async def _data_sync(self) -> tuple[str, dict]:
        assert self._object_store is not None
        root = Path(self._settings.data_path)
        files = await _to_thread(_list_files, root)
        for path in files:
            data = await _to_thread(path.read_bytes)
            await self._object_store.put_bytes(f"data/{path.name}", data)
        return f"synced {len(files)} raw input file(s) → data/", {"count": len(files)}

    # --- helpers -----------------------------------------------------------------------------

    async def _record_r2_job(
        self, agent: str, body: Callable[[], Awaitable[tuple[str, dict]]]
    ) -> None:
        try:
            run_id = await self._store.start(agent)
        except Exception:  # noqa: BLE001 — DB down at row-open: log + bail, never crash the caller
            logger.exception("could not open agent_run for %s; job skipped", agent)
            return
        try:
            if self._object_store is None:
                await self._store.finish(run_id, status=SKIPPED, summary="R2 not configured")
                return
            summary, details = await body()
            await self._store.finish(run_id, status=SUCCEEDED, summary=summary, details=details)
        except Exception as exc:  # noqa: BLE001 — a job ends as failed with context, never crashes
            logger.exception("backup job %s failed", agent)
            await self._store.finish(run_id, status=FAILED, error=f"{type(exc).__name__}: {exc}")

    def _stamp(self) -> str:
        return datetime.now(self._tz).strftime("%Y%m%dT%H%M%S")

    async def _default_pg_dump(self) -> bytes:
        return await _to_thread(_pg_dump_sync, self._settings.database_url)

    async def _default_bundle_inspector(self, data: bytes) -> Fingerprint:
        work_dir = Path(await _to_thread(tempfile.mkdtemp, prefix="drill-"))
        try:
            bundle_path = work_dir / "snapshot.bundle"
            await _to_thread(bundle_path.write_bytes, data)
            clone_dir = work_dir / "clone"
            # A corrupt bundle fails the clone (check=True) → the drill run fails.
            await GitRepo.clone_from(str(bundle_path), str(clone_dir))
            repo = GitRepo(str(clone_dir))
            if not await repo.verify_bundle(str(bundle_path)):
                raise DrillError("git bundle verify failed")
            return Fingerprint(
                head_sha=await repo.head_sha(),
                commit_count=await repo.commit_count(),
                file_count=await repo.tracked_file_count(),
            )
        finally:
            await _to_thread(shutil.rmtree, work_dir, True)


def _list_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file())


def _pg_dump_sync(database_url: str) -> bytes:
    result = subprocess.run(
        ["pg_dump", "--no-owner", "--no-privileges", "--format=plain", database_url],
        capture_output=True,
        timeout=600,
        check=True,
    )
    return result.stdout


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def build_backup_jobs(
    settings: Settings, db: Database, vault_backup: VaultBackupService
) -> BackupJobs:
    """Construct the durability jobs from settings + an (already-connected) db + vault backup.

    Shared by the CLI entrypoint (:mod:`app.cli`) and the in-process scheduler wiring
    (:mod:`app.main`) so both drive the same jobs. ``object_store`` is ``None`` when R2 creds
    are absent (dev) ⇒ the R2 jobs record a skipped run.
    """
    return BackupJobs(
        settings=settings,
        store=PgAgentRunStore(db),
        object_store=build_object_store(settings),
        vault_backup=vault_backup,
    )
