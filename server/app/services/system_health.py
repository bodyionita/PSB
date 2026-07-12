"""System health probes for GET /health: db, vault, git remote.

Kept out of the router so the checks stay unit-testable and the router just delegates.
Blocking filesystem/git work runs in a thread (CLAUDE.md rule 8).
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..db import Database


@dataclass(frozen=True)
class HealthReport:
    db: bool
    vault: bool
    git_remote: bool
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
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def check(self) -> HealthReport:
        db_ok = await self._db.healthcheck()
        vault_ok = await asyncio.to_thread(_vault_ok, self._settings.vault_path)
        git_ok = await asyncio.to_thread(_git_remote_ok, self._settings.vault_path)

        # In production every leg must be green. Locally the vault git remote is deferred to
        # the provisioning session (ADR-012), so it must not fail dev /health.
        if self._settings.environment == "production":
            ok = db_ok and vault_ok and git_ok
        else:
            ok = db_ok and vault_ok
        return HealthReport(db=db_ok, vault=vault_ok, git_remote=git_ok, ok=ok)
