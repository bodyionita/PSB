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
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..db import Database

logger = logging.getLogger(__name__)

# Run statuses.
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"

# Run trigger origin (M8, ADR-053 §5). A row is `scheduled` unless a manual endpoint opened it
# inside :func:`trigger_scope`, letting the merged Activity feed categorize by *origin* not table.
SCHEDULED = "scheduled"
MANUAL = "manual"


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


# --- Live-log run scope (M8, ADR-053 §1) --------------------------------------------------------
# The `app.*`/`INFO` log-capture handler (:mod:`app.services.run_logs`) tags every record with the
# **currently-executing run** — the innermost job whose row is open. We track that as an immutable
# run-id STACK held in a contextvar: ``start`` pushes the freshly-minted id, ``finish`` pops it, so
# a pipeline parent's own log lines tag the parent while a step's lines tag the step's child run
# (proper nesting, same ADR-047 §5 ambient pattern as `parent_run_id`). Immutable-tuple replacement
# keeps it **task-safe** — a concurrent manual run in a different task carries its own stack, and
# `asyncio.to_thread` copies the context so blocking work still logs under the right run. No job
# body changes: the store's ``start``/``finish`` own the push/pop, like ``record_child_run``.
_run_id_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "agent_run_id_stack", default=()
)


def current_run_id() -> str | None:
    """The innermost open run in this task, or ``None`` outside any run — the id the log-capture
    handler tags emitted records with."""
    stack = _run_id_stack.get()
    return stack[-1] if stack else None


def _push_run_id(run_id: str) -> None:
    _run_id_stack.set((*_run_id_stack.get(), run_id))


def _pop_run_id(run_id: str) -> None:
    # Remove this run wherever it sits (top under LIFO nesting; defensive filter otherwise) and
    # never mutate the shared tuple in place — replace it, so other tasks' stacks are untouched.
    _run_id_stack.set(tuple(r for r in _run_id_stack.get() if r != run_id))


def begin_run_scope(run_id: str) -> None:
    """Called by a store's ``start`` right after a run row is opened: register the run with the
    active :func:`child_run_scope` collector (ADR-047 §5) and make it the innermost log-capture
    scope (ADR-053 §1). One call so the real + fake stores behave identically."""
    record_child_run(run_id)
    _push_run_id(run_id)


def end_run_scope(run_id: str) -> None:
    """Called by a store's ``finish``: close the run's log-capture scope and flush/reap its buffer
    (ADR-053 §2). Safe to call even if the DB update raised — the run is over regardless."""
    _pop_run_id(run_id)
    _notify_run_finished(run_id)


# --- Run trigger origin (M8, ADR-053 §5) --------------------------------------------------------
# A run is `scheduled` by default; the manual-trigger endpoint runs the job inside `trigger_scope()`
# so the row this task opens is stamped `manual`. Ambient contextvar (not a passed arg) → no job
# body change, mirroring `parent_run_id`.
_trigger: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_run_trigger", default=SCHEDULED
)


def current_trigger() -> str:
    """The trigger origin (`scheduled`/`manual`) a run opened right now should be stamped with."""
    return _trigger.get()


@contextmanager
def trigger_scope(trigger: str) -> Iterator[None]:
    """Within this scope, every ``agent_runs`` row opened via ``start`` is stamped ``trigger``
    (the manual endpoint wraps its job call in ``trigger_scope(MANUAL)``)."""
    token = _trigger.set(trigger)
    try:
        yield
    finally:
        _trigger.reset(token)


# --- Run-finish observer (M8, ADR-053 §2) -------------------------------------------------------
# The log flusher needs to know when a run finishes so it can flush that run's remaining buffered
# lines immediately and reap the in-memory buffer (a long-lived process opens thousands of runs).
# `finish` calls this optional hook — decoupled: the store knows nothing about the flusher, and with
# no hook registered (unit tests, CLI) it is a no-op. Set once at app startup, cleared on shutdown.
_run_finish_hook: Callable[[str], None] | None = None


def set_run_finish_hook(hook: Callable[[str], None] | None) -> None:
    """Register (or clear, with ``None``) the callback invoked with a run id when a run finishes."""
    global _run_finish_hook
    _run_finish_hook = hook


def _notify_run_finished(run_id: str) -> None:
    hook = _run_finish_hook
    if hook is None:
        return
    try:
        hook(run_id)
    except Exception:  # noqa: BLE001 — a flusher hiccup must never break closing a run (rule 7)
        logger.exception("run-finish hook failed for run %s", run_id)


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
    trigger: str = SCHEDULED


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
        trigger=row["trigger"],
    )


class PgAgentRunStore:
    """asyncpg-backed agent_runs store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self, agent: str) -> str:
        # Under a pipeline step the ambient parent links this child row (ADR-047 §5); a bare job run
        # picks up ``None`` and stays parentless, exactly as before. The trigger origin (M8, §5) is
        # `manual` only inside `trigger_scope`, else `scheduled` — no job body sets it.
        parent = current_parent_run_id()
        trigger = current_trigger()
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO agent_runs (agent, status, parent_run_id, trigger)"
                " VALUES ($1, $2, $3, $4) RETURNING id",
                agent,
                RUNNING,
                parent,
                trigger,
            )
        run_id = str(row["id"])
        begin_run_scope(run_id)  # link to the pipeline parent + open the log-capture scope
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
        try:
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
        finally:
            # Close the log-capture scope + flush/reap the run's buffer even if the UPDATE raised
            # (rule 7: the run is over regardless of a DB hiccup on the close).
            end_run_scope(run_id)

    async def latest(self, agent: str, *, status: str | None = None) -> AgentRun | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, agent, status, started_at, finished_at, model_used,
                       fallback_used, summary, details, error, parent_run_id, trigger
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
                       fallback_used, summary, details, error, parent_run_id, trigger
                  FROM agent_runs
                 WHERE id = $1
                """,
                run_id,
            )
        return _row_to_run(row)
