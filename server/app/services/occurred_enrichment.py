"""The nightly ``occurred-enrichment`` flagger (ADR-056 §7, M8.2 Task 3-E).

A small nightly step that finds **undated content nodes** — live, non-``inbox/`` content nodes with
no ``occurred`` event date — and files an ``occurred-enrichment`` review item for each, asking the
user to tag the event time in natural language. The answer is resolved and applied on the Review
surface (:class:`~app.services.review_service.ReviewService`), not here — this job only *surfaces*
the gap (like graph-health counts it; ADR-056 §7 strengthens the ADR-049 dedup occurred-signal).

Idempotent (rule 6): a node that already has a decidable (``pending``/``maybe``)
``occurred-enrichment`` item is skipped, so re-running never piles duplicates. Bounded per run
(rule 9: ``occurred_enrichment_max_per_run``) so the review queue never floods on the first night of
a large graph. DB-only (candidate reads + review-queue writes + its own ``agent_runs`` row) — like
the maybe-digest / dedup-sweep / graph-health reporters — so it unit-tests against fakes and is a
plain ``nightly`` pipeline step.

**Scope note (coarse-dating).** ADR-056 §7 names *undated / coarsely-dated* nodes. This first cut
flags the unambiguous case — ``occurred_start IS NULL`` (the "graph-health already counts them"
signal). "Coarse" is genuinely underspecified against the day-granular ``occurred_*`` columns (a
legitimate year-long event is indistinguishable from a year-granular guess), so refining it into a
separate flag is a logged follow-up, not silently folded in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from ..config import Settings
from ..db import Database
from .agent_runs import FAILED, SUCCEEDED, AgentRunStore
from .review_queue import (
    DECIDABLE_STATUSES,
    KIND_OCCURRED_ENRICHMENT,
    ReviewItem,
    ReviewQueue,
)

logger = logging.getLogger(__name__)

# agent_runs.agent name — the visible activity-feed row + the ops-console roster/console key.
AGENT = "occurred-enrichment"


@dataclass(frozen=True)
class UndatedNode:
    """One undated content node to flag: its id + a human label (title, else store path)."""

    id: str
    title: str
    type: str


class OccurredEnrichmentStore(Protocol):
    """The narrow read surface the flagger runs over (read-only)."""

    async def undated_content_nodes(
        self, *, entity_types: list[str], inbox_prefix: str, decidable: list[str], limit: int
    ) -> list[UndatedNode]: ...


@dataclass
class OccurredEnrichmentOutcome:
    """One flagger pass — feeds the ``occurred-enrichment`` agent_runs row + tests."""

    filed: int = 0
    candidates: int = 0

    def summary(self) -> str:
        return (
            f"occurred-enrichment: filed {self.filed} review item(s) "
            f"for undated node(s) ({self.candidates} candidate(s) this pass)"
        )

    def as_dict(self) -> dict[str, int]:
        return {"filed": self.filed, "candidates": self.candidates}


class OccurredEnrichmentService:
    """Owns the nightly ``occurred-enrichment`` flagger (ADR-056 §7)."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: OccurredEnrichmentStore,
        review_queue: ReviewQueue,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._store = store
        self._review = review_queue
        self._runs = run_store

    async def run_scheduled(self) -> OccurredEnrichmentOutcome | None:
        """Scheduler/CLI entry point. Opens the run, files an item per undated node, closes it;
        never raises (rule 7). Returns the outcome for CLI logging, or ``None`` when the run
        couldn't be opened / failed (the scheduler ignores the return)."""
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
            logger.exception("could not open agent_runs row for occurred-enrichment; skipped")
            return None
        try:
            outcome = await self._collect_and_file()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
            return outcome
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("occurred-enrichment failed")
            await self._safe_finish(run_id, exc)
            return None

    async def _collect_and_file(self) -> OccurredEnrichmentOutcome:
        settings = self._settings
        candidates = await self._store.undated_content_nodes(
            entity_types=list(settings.entity_like_types),
            inbox_prefix=f"{settings.inbox_folder}/%",
            decidable=list(DECIDABLE_STATUSES),
            limit=settings.occurred_enrichment_max_per_run,
        )
        logger.info("occurred-enrichment: %d undated node(s) to flag", len(candidates))
        filed = 0
        for node in candidates:
            try:
                await self._review.enqueue(
                    ReviewItem(
                        kind=KIND_OCCURRED_ENRICHMENT,
                        payload={"node_id": node.id, "title": node.title, "type": node.type},
                        excerpt=node.title or node.id,
                        source=AGENT,
                        source_ref=node.id,
                    )
                )
                filed += 1
            except Exception:  # noqa: BLE001 — one bad enqueue never aborts the pass (rule 7)
                logger.exception("occurred-enrichment: could not file item for %s", node.id)
        return OccurredEnrichmentOutcome(filed=filed, candidates=len(candidates))

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="occurred-enrichment failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close occurred-enrichment agent_runs row %s", run_id)


class PgOccurredEnrichmentStore:
    """asyncpg-backed candidate read — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def undated_content_nodes(
        self, *, entity_types: list[str], inbox_prefix: str, decidable: list[str], limit: int
    ) -> list[UndatedNode]:
        # Live, non-`inbox/` CONTENT nodes (type not an entity hub) with no occurred, that do NOT
        # already have a decidable occurred-enrichment review item (idempotent — rule 6). Recent
        # first, bounded (rule 9). `payload->>'node_id'` matches the id the flagger files below.
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT n.id, n.title, n.type
                  FROM nodes n
                 WHERE n.merged_into IS NULL
                   AND n.occurred_start IS NULL
                   AND n.store_path NOT LIKE $2
                   AND NOT (n.type = ANY($1::text[]))
                   AND NOT EXISTS (
                       SELECT 1 FROM review_queue r
                        WHERE r.kind = $3
                          AND r.status = ANY($4::text[])
                          AND r.payload->>'node_id' = n.id::text
                   )
                 ORDER BY n.node_created_at DESC NULLS LAST, n.id
                 LIMIT $5
                """,
                entity_types,
                inbox_prefix,
                KIND_OCCURRED_ENRICHMENT,
                decidable,
                limit,
            )
        return [
            UndatedNode(id=str(r["id"]), title=r["title"] or str(r["id"]), type=r["type"])
            for r in rows
        ]


def build_occurred_enrichment_service(
    settings: Settings, db: Database
) -> OccurredEnrichmentService:
    """Construct a standalone flagger for the nightly pipeline step + the manual
    ``POST /agents/occurred-enrichment/run`` trigger. DB-only (candidate reads + review writes + its
    own run row, no store git) — like the maybe-digest / graph-health reporters."""
    from .agent_runs import PgAgentRunStore
    from .review_queue import PgReviewQueue

    return OccurredEnrichmentService(
        settings=settings,
        store=PgOccurredEnrichmentStore(db),
        review_queue=PgReviewQueue(db),
        run_store=PgAgentRunStore(db),
    )
