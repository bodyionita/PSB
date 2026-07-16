"""The weekly maybe-digest job (M6 task 8, 04-pipelines §Scheduling, ADR-048 §8).

A parked ``maybe`` review item has no expiry (ADR-048 addendum b) — but an untriaged pile silently
stalls the feature. This weekly job emits **one feed-visible ``agent_run``** summarizing the parked
maybes (total, per-kind breakdown, age of the oldest) so they stay visible; the Review UI already
carries the count badge + per-card aging (M6 task 7). **No push** — that is M10.

It only reads (a cheap ``GROUP BY`` over ``review_queue``) and writes its own run row; it never
touches the graph or the store. Never raises (rule 7) and depends on a narrow store protocol so it
unit-tests against a fake (no live DB — 08 testing policy). A ``weekly`` pipeline step (M6 task 8);
this also ships the ``maybe-digest`` CLI verb (the run-now + local-test path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ..config import Settings
from .agent_runs import FAILED, SUCCEEDED, AgentRunStore
from .review_queue import MaybeKindStat

logger = logging.getLogger(__name__)

# agent_runs.agent name for the digest (the visible activity-feed row, vision P8).
AGENT = "maybe-digest"


class MaybeDigestStore(Protocol):
    """The narrow slice of the review queue the digest reads: the parked-``maybe`` aggregate."""

    async def maybe_kind_stats(self) -> list[MaybeKindStat]: ...


@dataclass
class MaybeDigestOutcome:
    """One digest run's result — the ``maybe-digest`` agent_runs summary + details (vision P8)."""

    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    oldest_created_at: datetime | None = None
    oldest_age_days: int | None = None

    def summary(self) -> str:
        if self.total == 0:
            return "maybe digest: no parked maybes"
        kinds = len(self.by_kind)
        base = (
            f"maybe digest: {self.total} parked maybe(s) across {kinds} kind(s)"
        )
        if self.oldest_age_days is not None:
            base += f", oldest {self.oldest_age_days}d old"
        return base

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "by_kind": self.by_kind,
            "oldest_created_at": (
                self.oldest_created_at.isoformat() if self.oldest_created_at is not None else None
            ),
            "oldest_age_days": self.oldest_age_days,
        }


class MaybeDigestService:
    """Owns the weekly maybe-digest: aggregate the parked maybes → a feed-visible run (ADR-048 §8).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        store: MaybeDigestStore,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._store = store
        self._runs = run_store

    async def run_scheduled(self) -> None:
        """The scheduler/CLI entry point. Opens the run, aggregates, closes it; never raises (P8).

        A run is opened **even when nothing is parked** — the empty digest is itself the signal
        ("nothing to triage this week"), and the feed reads as a heartbeat rather than a gap."""
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
            logger.exception("could not open agent_runs row for maybe digest; skipped")
            return
        try:
            outcome = await self._aggregate()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("maybe digest failed")
            await self._safe_finish(run_id, exc)

    async def _aggregate(self) -> MaybeDigestOutcome:
        stats = await self._store.maybe_kind_stats()
        outcome = MaybeDigestOutcome(
            total=sum(s.count for s in stats),
            by_kind={s.kind: s.count for s in stats},
        )
        if stats:
            oldest = min(s.oldest_created_at for s in stats)
            outcome.oldest_created_at = oldest
            # Naive stamps are treated as UTC so the age never mixes aware/naive (the DB column is
            # timestamptz → aware, but a fake store may hand back naive times in a unit test).
            aware = oldest if oldest.tzinfo is not None else oldest.replace(tzinfo=UTC)
            outcome.oldest_age_days = max((datetime.now(UTC) - aware).days, 0)
        return outcome

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="maybe digest failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close maybe-digest agent_runs row %s", run_id)


def build_maybe_digest_service(settings: Settings, db) -> MaybeDigestService:
    """Construct a standalone maybe-digest for the CLI (``python -m app.cli maybe-digest``) / the
    weekly pipeline step (M6 task 8). DB-only (a read over ``review_queue`` + its own run row)."""
    from .agent_runs import PgAgentRunStore
    from .review_queue import PgReviewQueue

    return MaybeDigestService(
        settings=settings,
        store=PgReviewQueue(db),
        run_store=PgAgentRunStore(db),
    )
