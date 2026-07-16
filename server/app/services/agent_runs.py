"""agent_runs persistence (02-data-model §3, vision P8 "everything visible").

Every scheduled/background job opens an ``agent_runs`` row (``running``) and closes it
(``succeeded`` / ``failed`` / ``skipped``) with a human-readable summary + a ``details`` JSON blob.
The durability jobs use this for the activity feed and the ``/health`` ``backups`` leg reads the
latest ``integrity-drill`` run from it (ADR-014 §6).

Plain SQL over asyncpg (rule 5); the jobs depend on the :class:`AgentRunStore` protocol so they
unit-test against an in-memory fake (no live DB in CI).
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..db import Database

# Run statuses.
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"


# --- Pipeline parent/child linkage (ADR-047 §5) -------------------------------------------------
# The pipeline runner (:mod:`app.services.pipeline`) opens a *parent* run, then executes each step
# inside :func:`child_run_scope`. While that scope is active, every ``agent_runs`` row a step opens
# via ``start`` links to the parent (``parent_run_id``) and its id is captured in the scope's
# collector — so the runner can read each step's own child run back to honour ``on_fail`` and record
# the per-step sequence. A bare job run (no active scope) opens a parentless row, unchanged.
#
# contextvars (not a passed argument) so **no job changes what it does** (ADR-047 consequences): a
# job's ``run_scheduled`` keeps calling ``start(AGENT)`` with no idea it runs under a pipeline; the
# ambient parent is picked up transparently. The scope is task-local, so a concurrent manual run
# firing outside the scope is never miscaptured as a child.
_parent_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_run_parent_id", default=None
)
_child_run_ids: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "agent_run_child_ids", default=None
)


def current_parent_run_id() -> str | None:
    """The ambient parent run id, or ``None`` when not running under a pipeline step."""
    return _parent_run_id.get()


def record_child_run(run_id: str) -> None:
    """Register a freshly-opened run id with the active :func:`child_run_scope` collector (no-op
    outside a scope). Called by every store's ``start`` so both the real and fake stores link
    identically."""
    collector = _child_run_ids.get()
    if collector is not None:
        collector.append(run_id)


@contextmanager
def child_run_scope(parent_run_id: str) -> Iterator[list[str]]:
    """Runner-side: within this scope every ``agent_runs`` row opened via ``start`` links to
    ``parent_run_id`` and its id is appended to the yielded list. Nested/re-entrant safe via the
    contextvar tokens; the collector is fresh per scope so each step captures only its own children.
    """
    collected: list[str] = []
    parent_token = _parent_run_id.set(parent_run_id)
    child_token = _child_run_ids.set(collected)
    try:
        yield collected
    finally:
        _child_run_ids.reset(child_token)
        _parent_run_id.reset(parent_token)


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
    parent_run_id: str | None = None


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

    async def get(self, run_id: str) -> AgentRun | None: ...


def _details(value: Any) -> dict[str, Any]:
    # asyncpg returns jsonb as text by default; tolerate both text and already-decoded dicts.
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _row_to_run(row: Any) -> AgentRun | None:
    if row is None:
        return None
    parent = row["parent_run_id"]
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
        parent_run_id=str(parent) if parent is not None else None,
    )


class PgAgentRunStore:
    """asyncpg-backed agent_runs store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self, agent: str) -> str:
        # Under a pipeline step the ambient parent links this child row (ADR-047 §5); a bare job run
        # picks up ``None`` and stays parentless, exactly as before.
        parent = current_parent_run_id()
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO agent_runs (agent, status, parent_run_id) VALUES ($1, $2, $3)"
                " RETURNING id",
                agent,
                RUNNING,
                parent,
            )
        run_id = str(row["id"])
        record_child_run(run_id)
        return run_id

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
                       fallback_used, summary, details, error, parent_run_id
                  FROM agent_runs
                 WHERE agent = $1 AND ($2::text IS NULL OR status = $2)
                 ORDER BY started_at DESC
                 LIMIT 1
                """,
                agent,
                status,
            )
        return _row_to_run(row)

    async def get(self, run_id: str) -> AgentRun | None:
        # A malformed/non-uuid id is rejected at the router (uuid path type), so this only sees
        # well-formed ids; an unknown one → None → 404. Reads the full row incl. details for the
        # Admin tab's run-status poll (03-api §Activity feed; M2 pull-forward of the M4 feed).
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, agent, status, started_at, finished_at, model_used,
                       fallback_used, summary, details, error, parent_run_id
                  FROM agent_runs
                 WHERE id = $1
                """,
                run_id,
            )
        return _row_to_run(row)
