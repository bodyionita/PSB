"""System health probes for GET /health: db, vault, git remote, backups.

Kept out of the router so the checks stay unit-testable and the router just delegates.
Blocking filesystem/git work runs in a thread (CLAUDE.md rule 8).
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import Settings
from ..db import Database
from .agent_runs import FAILED, SUCCEEDED, AgentRunStore, PgAgentRunStore
from .backup_jobs import INTEGRITY_DRILL


@dataclass(frozen=True)
class HealthReport:
    db: bool
    vault: bool
    git_remote: bool
    backups: bool
    ok: bool


def _vault_ok(vault_path: str) -> bool:
    path = Path(vault_path)
    return path.is_dir()


def _git_remote_ok(vault_path: str) -> bool:
    """True if the vault is a git repo with at least one configured remote."""
    path = Path(vault_path)
    if not (path / ".git").exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


class SystemHealth:
    def __init__(
        self, db: Database, settings: Settings, agent_runs: AgentRunStore | None = None
    ) -> None:
        self._db = db
        self._settings = settings
        self._agent_runs = agent_runs or PgAgentRunStore(db)

    async def check(self) -> HealthReport:
        db_ok = await self._db.healthcheck()
        vault_ok = await asyncio.to_thread(_vault_ok, self._settings.vault_path)
        git_ok = await asyncio.to_thread(_git_remote_ok, self._settings.vault_path)
        backups_ok = await self._backups_ok()

        # In production every leg must be green. Locally the vault git remote and the backup
        # drill are deferred to the provisioning session (ADR-012), so they must not fail dev
        # /health — both are reported but gated to prod, exactly like git_remote.
        if self._settings.environment == "production":
            ok = db_ok and vault_ok and git_ok and backups_ok
        else:
            ok = db_ok and vault_ok
        return HealthReport(
            db=db_ok, vault=vault_ok, git_remote=git_ok, backups=backups_ok, ok=ok
        )

    async def _backups_ok(self) -> bool:
        """False when the latest integrity drill failed or the last good one is overdue (§6).

        A currently-``running`` drill doesn't flip health: we degrade only on an explicit
        failure or on the last *succeeded* drill being older than the configured max age.
        """
        try:
            latest = await self._agent_runs.latest(INTEGRITY_DRILL)
            if latest is None:
                return False  # never drilled yet
            if latest.status == FAILED:
                return False
            last_good = await self._agent_runs.latest(INTEGRITY_DRILL, status=SUCCEEDED)
            if last_good is None or last_good.started_at is None:
                return False
            max_age = timedelta(days=self._settings.integrity_drill_max_age_days)
            return datetime.now(UTC) - last_good.started_at <= max_age
        except Exception:  # noqa: BLE001 — a DB blip on the agent_runs read shouldn't error /health
            return False
