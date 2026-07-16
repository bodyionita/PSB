"""The nightly inbox drainer (M6 task 6, 04-pipelines §3b, ADR-048 §10).

Captures whose organize was down — or produced no valid nodes — at ingest are never lost: they
materialize a single ``inbox/`` fallback node (CLAUDE.md rule 2). This job re-runs the **existing**
``reorganize_capture`` over each such capture with the now-richer entity registry, so a previously
unorganizable capture may resolve into real typed nodes. Notes are replaced **only on success**; a
still-failing capture keeps its ``inbox/`` node (and is retried next night). Residual entity
ambiguity files the normal ``entity-ambiguity`` items — no new review kind (ADR-048 §10).

Idempotent (rule 6) and **bounded per run** (``inbox_drain_max_per_run``): a run re-organizes at
most that many captures, oldest-first; any remainder waits for the next run. Every run lands in
``agent_runs`` (vision P8) and it never raises (rule 7) — one bad capture never aborts the sweep.
A one-tap-removed capture is doubly excluded (the store filter + ``reorganize``'s own ``removed_at``
skip), so a removed memory can't resurrect through the drainer.

Depends on narrow protocols (capture reader + reorganizer + run store) so it unit-tests against
fakes (no live DB/LLM — 08 testing policy). Scheduling is a ``nightly`` pipeline step (M6 task 8);
this ships the job + the ``inbox-drain`` CLI verb (the run-now + local-test path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from ..config import Settings
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.capture_store import CaptureRecord

logger = logging.getLogger(__name__)

# agent_runs.agent name for the drainer (the visible activity-feed row, vision P8).
AGENT = "inbox-drain"


class InboxCaptureReader(Protocol):
    """The narrow slice of the capture store the drainer reads: the inbox-materialized captures to
    re-organize, plus a by-id re-fetch to see whether a reorganize resolved out of ``inbox/``."""

    async def list_inbox_materialized(self, *, folder: str, limit: int) -> list[CaptureRecord]: ...

    async def get(self, capture_id: str) -> CaptureRecord | None: ...


class Reorganizer(Protocol):
    """The narrow slice of the capture pipeline the drainer drives: re-organize one capture inline
    (blocking) through the single writer (rule 2b), replacing its notes only on success."""

    async def reorganize_capture_now(self, capture_id: str) -> None: ...


@dataclass
class InboxDrainOutcome:
    """One drain run's aggregate — the ``inbox-drain`` agent_runs summary + details (vision P8)."""

    found: int = 0
    reorganized: int = 0
    resolved: int = 0
    errored: int = 0
    truncated: bool = False  # the run hit the per-run cap; the remainder is deferred (not lost)

    @property
    def still_inbox(self) -> int:
        """Re-organized captures that did NOT resolve — organize is still down / still can't type
        them, so they kept their ``inbox/`` node and re-qualify next run."""
        return self.reorganized - self.resolved

    def summary(self) -> str:
        base = (
            f"inbox drain: {self.resolved}/{self.reorganized} re-organized capture(s) resolved out "
            f"of inbox ({self.still_inbox} still unresolved)"
        )
        if self.errored:
            base += f"; {self.errored} errored (skipped)"
        if self.truncated:
            base += "; more remain (deferred to the next run)"
        return base

    def as_dict(self) -> dict[str, object]:
        return {
            "found": self.found,
            "reorganized": self.reorganized,
            "resolved": self.resolved,
            "still_inbox": self.still_inbox,
            "errored": self.errored,
            "truncated": self.truncated,
        }


class InboxDrainService:
    """Owns the nightly inbox drain: find inbox-materialized captures → re-organize each → tally."""

    def __init__(
        self,
        *,
        settings: Settings,
        capture_store: InboxCaptureReader,
        pipeline: Reorganizer,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._store = capture_store
        self._pipeline = pipeline
        self._runs = run_store

    async def run_scheduled(self) -> None:
        """The scheduler/CLI entry point. Opens the run, drains, closes it; never raises (P8)."""
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
            logger.exception("could not open agent_runs row for inbox drain; skipped")
            return
        try:
            outcome = await self._drain()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("inbox drain failed")
            await self._safe_finish(run_id, exc)

    async def _drain(self) -> InboxDrainOutcome:
        folder = self._settings.inbox_folder
        limit = self._settings.inbox_drain_max_per_run
        captures = await self._store.list_inbox_materialized(folder=folder, limit=limit)
        outcome = InboxDrainOutcome(found=len(captures), truncated=len(captures) >= limit)
        for record in captures:
            # Best-effort per capture (rule 7): `reorganize_capture_now` manages its own agent_runs
            # row + never raises past its guard, but a defensive catch keeps a surprise from
            # aborting the sweep (the rest of the roster still drains).
            try:
                await self._pipeline.reorganize_capture_now(record.id)
            except Exception:  # noqa: BLE001 — one bad capture never aborts the sweep
                logger.exception(
                    "inbox drain: reorganize of capture %s failed (skipped)", record.id
                )
                outcome.errored += 1
                continue
            outcome.reorganized += 1
            # Re-fetch to see whether the fresh organize replaced the inbox node with real typed
            # nodes. On the inbox fallback (organize still down / still no valid nodes) the old node
            # is kept, so node_paths stay under `inbox/` — counted as still-unresolved.
            fresh = await self._store.get(record.id)
            if fresh is not None and not _still_in_inbox(fresh.node_paths, folder):
                outcome.resolved += 1
        return outcome

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="inbox drain failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close inbox-drain agent_runs row %s", run_id)


def _still_in_inbox(node_paths: list[str], folder: str) -> bool:
    """Whether any recorded node path is still under the ``<folder>/`` inbox — i.e. the reorganize
    did not resolve the capture into real typed nodes (matches the store's selection predicate)."""
    prefix = f"{folder}/"
    return any(p.startswith(prefix) for p in node_paths)


def build_inbox_drain_service(settings: Settings, db, store_backup) -> InboxDrainService:
    """Construct a standalone drainer for the CLI (``python -m app.cli inbox-drain``) / the nightly
    pipeline step (M6 task 8). It drives the **real** capture pipeline (the single writer, rule 2b —
    built with ``build_capture_pipeline``, so it needs the store backup for the on-success commit)
    and shares the DB-backed capture store + run store."""
    from ..services.agent_runs import PgAgentRunStore
    from ..services.capture_pipeline import build_capture_pipeline
    from ..services.capture_store import PgCaptureStore

    return InboxDrainService(
        settings=settings,
        capture_store=PgCaptureStore(db),
        pipeline=build_capture_pipeline(settings, db, store_backup),
        run_store=PgAgentRunStore(db),
    )
