"""In-process JobRunner — single-flight guard + manual-trigger scope (M8 task 1, ADR-053 §7).

One guard the scheduler and the manual endpoint both route through. It is **authoritative** because
the scheduler must run single-process (two schedulers would double-fire), so no cross-worker race is
possible — a plain in-process running-set, mutated only between awaits on the one event loop.

- **Manual trigger** (``POST /agents/{name}/run``, T3) → :meth:`run_manual`: takes the slot or else
  raises :class:`JobAlreadyRunning` (the endpoint maps it to ``409``), and stamps the run ``manual``
  via :func:`~app.services.agent_runs.trigger_scope` so the feed files it under *manual actions*.
- **Scheduler step** (the pipeline runner) → :meth:`scheduled_step`: advisory-marks the step running
  so a concurrent manual trigger of the same agent ``409``s; if a manual run already holds the slot,
  the step is **skipped** (single-flight; the manual run is doing the work, and every job is
  idempotent — rule 6). It never *raises* into the pipeline.

The seam owns only the guard + the trigger scope. The ``_current_run_id`` log scope is owned by the
``agent_runs`` store (it mints the run id), so job bodies stay untouched (ADR-053 §7).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from .agent_runs import MANUAL, trigger_scope

T = TypeVar("T")


class JobAlreadyRunning(RuntimeError):
    """The named agent (or a pipeline step of it) is already running — the manual trigger is a
    no-op single-flight conflict (``409`` at the endpoint)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"job {name!r} is already running")
        self.name = name


class JobRunner:
    def __init__(self) -> None:
        self._running: set[str] = set()

    def is_running(self, name: str) -> bool:
        return name in self._running

    def running_agents(self) -> frozenset[str]:
        """Snapshot of the agents currently running (scheduled steps + manual runs) — the roster
        endpoint (T3) reads this for live status."""
        return frozenset(self._running)

    def _acquire(self, name: str) -> bool:
        # Sync, no await between the check and the add → atomic on the single event loop.
        if name in self._running:
            return False
        self._running.add(name)
        return True

    def _release(self, name: str) -> None:
        self._running.discard(name)

    @asynccontextmanager
    async def scheduled_step(self, name: str):
        """Advisory-mark a scheduler step running. Yields ``True`` if this call took the slot, or
        ``False`` when a manual run already holds it (the pipeline runner then skips the step)."""
        acquired = self._acquire(name)
        try:
            yield acquired
        finally:
            if acquired:
                self._release(name)

    async def run_manual(self, name: str, job: Callable[[], Awaitable[T]]) -> T:
        """Run a manual job under single-flight + the ``manual`` trigger origin. Raises
        :class:`JobAlreadyRunning` if the agent is already running (scheduled or manual)."""
        if not self._acquire(name):
            raise JobAlreadyRunning(name)
        try:
            with trigger_scope(MANUAL):
                return await job()
        finally:
            self._release(name)
