"""Startup migration check (ADR-011): warn — never block, never auto-apply in prod.

Deliberately does NOT import Alembic/SQLAlchemy: they are migration-only dependencies and
must not leak into the runtime (CLAUDE.md rule 5). The head revision is derived by reading
the plain-text revision files; the DB's current revision comes from ``alembic_version``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .db import Database

logger = logging.getLogger(__name__)

_REVISION_RE = re.compile(r"^revision\s*(?::\s*[^=]+)?=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN_RE = re.compile(r"^down_revision\s*(?::\s*[^=]+)?=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def compute_head(versions_dir: Path = _VERSIONS_DIR) -> str | None:
    """The revision that no other revision points back to (the tip of a linear history)."""
    revisions: set[str] = set()
    down_revisions: set[str] = set()
    for file in versions_dir.glob("*.py"):
        text = file.read_text(encoding="utf-8")
        rev = _REVISION_RE.search(text)
        if rev:
            revisions.add(rev.group(1))
        down = _DOWN_RE.search(text)
        if down:
            down_revisions.add(down.group(1))
    heads = revisions - down_revisions
    if len(heads) == 1:
        return next(iter(heads))
    return None  # empty, or a branch we won't guess at


async def current_db_revision(db: Database) -> str | None:
    try:
        async with db.acquire() as conn:
            return await conn.fetchval("SELECT version_num FROM alembic_version")
    except Exception:
        return None  # table absent => DB never migrated


async def warn_if_behind_head(db: Database) -> None:
    head = compute_head()
    current = await current_db_revision(db)
    if head is None:
        return
    if current is None:
        logger.warning(
            "Database has no Alembic revision; run `alembic upgrade head` (expected %s).", head
        )
    elif current != head:
        logger.warning(
            "Database at revision %s is behind head %s; run `alembic upgrade head`.",
            current,
            head,
        )
    else:
        logger.info("Database schema at head (%s).", head)
