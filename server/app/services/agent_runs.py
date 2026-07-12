"""agent_runs persistence (02-data-model §3, vision P8 "everything visible").

Every scheduled/background job opens an ``agent_runs`` row (``running``) and closes it
(``succeeded`` / ``failed`` / ``skipped``) with a human-readable summary + a ``details`` JSON blob.
The durability jobs use this for the activity feed and the ``/health`` ``backups`` leg reads the
latest ``integrity-drill`` run from it (ADR-014 §6).

Plain SQL over asyncpg (rule 5); the jobs depend on the :class:`AgentRunStore` protocol so they
unit-test against an in-memory fake (no live DB in CI).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..db import Database

# Run statuses.
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class AgentRun:
    id: str
    agent: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_used: str | None = None
    fallback_used: bool = False
    summary: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class AgentRunStore(Protocol):
    async def start(self, agent: str) -> str: ...

    async def finish(
        self,
        run_id: str,
        *,
        status: str,
        summary: str | None = None,
        details: dict[str, Any] | None = None,
        error: str | None = None,
        model_used: str | None = None,
        fallback_used: bool = False,
    ) -> None: ...

    async def latest(self, agent: str, *, status: str | None = None) -> AgentRun | None: ...


def _details(value: Any) -> dict[str, Any]:
    # asyncpg returns jsonb as text by default; tolerate both text and already-decoded dicts.
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


class PgAgentRunStore:
    """asyncpg-backed agent_runs store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self, agent: str) -> str:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO agent_runs (agent, status) VALUES ($1, $2) RETURNING id",
                agent,
                RUNNING,
            )
        return str(row["id"])

    async def finish(
        self,
        run_id: str,
        *,
        status: str,
        summary: str | None = None,
        details: dict[str, Any] | None = None,
        error: str | None = None,
        model_used: str | None = None,
        fallback_used: bool = False,
    ) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_runs
                   SET status = $2, summary = $3, details = $4::jsonb,
                       error = $5, model_used = $6, fallback_used = $7, finished_at = now()
                 WHERE id = $1
                """,
                run_id,
                status,
                summary,
                json.dumps(details or {}),
                error,
                model_used,
                fallback_used,
            )

    async def latest(self, agent: str, *, status: str | None = None) -> AgentRun | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, agent, status, started_at, finished_at, model_used,
                       fallback_used, summary, details, error
                  FROM agent_runs
                 WHERE agent = $1 AND ($2::text IS NULL OR status = $2)
                 ORDER BY started_at DESC
                 LIMIT 1
                """,
                agent,
                status,
            )
        if row is None:
            return None
        return AgentRun(
            id=str(row["id"]),
            agent=row["agent"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            model_used=row["model_used"],
            fallback_used=row["fallback_used"],
            summary=row["summary"],
            details=_details(row["details"]),
            error=row["error"],
        )
