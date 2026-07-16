"""The nightly dedup sweep job (M6 task 5, 04-pipelines §3b, ADR-049).

Recently-ingested **content** nodes whose HNSW top-K neighbour clears a strict AND of high cosine +
a shared canonical edge to a common entity hub + occurred-overlap file a ``dedup-proposal`` review
item the user resolves with merge / keep / link (ADR-049 §3). The job is watermarked off
``agent_runs`` (the backfill idiom — no dedicated table, ADR-049 §4); a **re-file guard** (skip any
pair with an existing ``dedup-proposal`` in any status, ADR-049 §5) makes a run-overlap re-scan
harmless. It only *files* items — the merge/keep/link actions happen later at resolution
(``ReviewService`` → the shared ``MergeCore``), so the sweep is lightweight (no store writes).

Never raises (rule 7); depends on protocols so it unit-tests against fakes (08 testing policy).
Scheduling is a ``nightly`` pipeline step (M6 task 8) — this ships the job + the ``dedup-sweep`` CLI
verb (the run-now + local-test path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..services.agent_runs import FAILED, SUCCEEDED, AgentRunStore
from ..services.review_queue import KIND_DEDUP_PROPOSAL, ReviewItem, ReviewQueue
from ..vocab.service import VocabularyProvider, effective_vocabulary
from .store import DedupStore, NodeMergeStat

logger = logging.getLogger(__name__)

# agent_runs.agent name for the sweep (the watermark source + visible in the activity feed, P8).
AGENT = "dedup-sweep"


@dataclass(frozen=True)
class DedupOutcome:
    """Result of one sweep — feeds the ``dedup-sweep`` agent_runs row + tests."""

    pairs_scanned: int = 0
    proposals_filed: int = 0
    already_filed: int = 0

    def summary(self) -> str:
        return (
            f"dedup sweep: {self.proposals_filed} proposal(s) filed across {self.pairs_scanned} "
            f"candidate pair(s), {self.already_filed} already filed (skipped)"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "pairs_scanned": self.pairs_scanned,
            "proposals_filed": self.proposals_filed,
            "already_filed": self.already_filed,
        }


class DedupSweepService:
    """Owns the nightly dedup sweep: candidates → canonicalize/dedup → re-file guard → enqueue."""

    def __init__(
        self,
        *,
        settings: Settings,
        dedup_store: DedupStore,
        review_queue: ReviewQueue,
        run_store: AgentRunStore,
        vocab: VocabularyProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = dedup_store
        self._review = review_queue
        self._runs = run_store
        # Effective vocabulary (seeds ∪ approved additions — ADR-027/035); None ⇒ seeds. Content
        # types = node_types minus entity_like_types; both are read live so a governance change is
        # forward-live (a newly approved content type joins the sweep next run).
        self._vocab = vocab

    async def run_scheduled(self) -> None:
        """The scheduler/CLI entry point. Opens the run, sweeps, closes it; never raises (P8)."""
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
            logger.exception("could not open agent_runs row for dedup sweep; skipped")
            return
        try:
            outcome = await self._scan()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("dedup sweep failed")
            await self._safe_finish(run_id, exc)

    async def _scan(self) -> DedupOutcome:
        now = datetime.now(UTC)
        watermark = await self._watermark(now)
        effective = await effective_vocabulary(self._vocab, self._settings)
        entity_types = list(effective.entity_like_types)
        content_types = [t for t in effective.node_types if t not in set(entity_types)]

        candidates = await self._store.candidate_pairs(
            content_types=content_types,
            entity_like_types=entity_types,
            watermark=watermark,
            min_cosine=self._settings.dedup_min_cosine,
            candidate_k=self._settings.dedup_candidate_k,
        )

        # Canonicalize each directional pair to (least, greatest) + de-duplicate — the same pair can
        # surface from both drivers (ADR-049 §4). Symmetric signals (cosine/shared-entity/overlap)
        # need no reorientation; the first sighting wins (ordered cosine-desc, so the strongest).
        pairs: dict[tuple[str, str], _CanonicalPair] = {}
        titles: dict[str, str | None] = {}
        for c in candidates:
            titles.setdefault(c.node_a, c.title_a)
            titles.setdefault(c.node_b, c.title_b)
            a, b = (c.node_a, c.node_b) if c.node_a < c.node_b else (c.node_b, c.node_a)
            pairs.setdefault(
                (a, b),
                _CanonicalPair(
                    node_a=a,
                    node_b=b,
                    cosine=c.cosine,
                    shared_entity_ids=c.shared_entity_ids,
                    shared_entity_titles=c.shared_entity_titles,
                    occurred_overlap=c.occurred_overlap,
                ),
            )

        stats = await self._store.survivor_stats(
            sorted({nid for pair in pairs for nid in pair})
        )
        max_pairs = self._settings.dedup_max_pairs_per_run
        filed = 0
        already = 0
        for (a, b), pair in pairs.items():
            if filed >= max_pairs:
                break
            # Re-file guard (ADR-049 §5): a decided (kept/linked) or merged pair never re-proposed.
            if await self._store.proposal_exists(a, b):
                already += 1
                continue
            survivor = default_survivor(a, b, stats)
            await self._review.enqueue(
                ReviewItem(
                    kind=KIND_DEDUP_PROPOSAL,
                    payload={
                        "node_a": a,
                        "node_b": b,
                        "signals": {
                            "cosine": round(pair.cosine, 4),
                            "shared_entity_ids": pair.shared_entity_ids,
                            "shared_entity_titles": pair.shared_entity_titles,
                            "occurred_overlap": pair.occurred_overlap,
                        },
                        "default_survivor": survivor,
                    },
                    excerpt=_excerpt(titles.get(a), titles.get(b), pair.cosine),
                    source=AGENT,
                )
            )
            filed += 1
        return DedupOutcome(
            pairs_scanned=len(pairs), proposals_filed=filed, already_filed=already
        )

    async def _watermark(self, now: datetime) -> datetime:
        """Examine only content nodes indexed since the last successful sweep (the backfill idiom);
        first run / a run-store hiccup falls back to the window (ADR-049 §4). The re-file guard
        makes the run-overlap re-scan harmless, so the watermark needs no safety margin."""
        default = now - timedelta(days=self._settings.dedup_window_days)
        try:
            last = await self._runs.latest(AGENT, status=SUCCEEDED)
        except Exception:  # noqa: BLE001 — a run-store read hiccup falls back to the window
            return default
        if last is None or last.started_at is None:
            return default
        return last.started_at

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="dedup sweep failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close dedup-sweep agent_runs row %s", run_id)


@dataclass(frozen=True)
class _CanonicalPair:
    """A canonicalized ``(least, greatest)`` pair + its (symmetric) signals, ready to file."""

    node_a: str
    node_b: str
    cosine: float
    shared_entity_ids: list[str]
    shared_entity_titles: list[str]
    occurred_overlap: bool


# A node with no stats sorts as newest / degree-0 (loses the survivor pick), and its age key is the
# far future so a *dated* node always wins the "keep the older original" tiebreak (ADR-049 §6).
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def default_survivor(a: str, b: str, stats: dict[str, NodeMergeStat]) -> str:
    """The default survivor of a canonical pair (ADR-049 §6): **higher canonical degree**, then
    **older** ``node_created_at`` (fallback ``indexed_at``) — keep the original, fold the newer
    duplicate into it. A final id tiebreak keeps the choice deterministic across runs."""
    sa, sb = stats.get(a), stats.get(b)
    deg_a = sa.degree if sa else 0
    deg_b = sb.degree if sb else 0
    if deg_a != deg_b:
        return a if deg_a > deg_b else b
    age_a, age_b = _age_key(sa), _age_key(sb)
    if age_a != age_b:
        return a if age_a < age_b else b
    return min(a, b)


def _age_key(stat: NodeMergeStat | None) -> datetime:
    """The node's age for the survivor tiebreak — ``node_created_at`` else ``indexed_at`` else the
    far future (an unknown-age node never beats a dated one for 'older'). Naive times are treated as
    UTC so the comparison never mixes aware/naive."""
    if stat is None:
        return _FAR_FUTURE
    stamp = stat.node_created_at or stat.indexed_at
    if stamp is None:
        return _FAR_FUTURE
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _excerpt(title_a: str | None, title_b: str | None, cosine: float) -> str:
    """A short decidable-in-place hint for the review list (the two node titles + the cosine). The
    payload carries the node ids + signals; this is just the human line the list shows."""
    left = title_a or "(untitled)"
    right = title_b or "(untitled)"
    return f'possible duplicate: "{left}" ~ "{right}" (cosine {cosine:.2f})'


def build_dedup_sweep_service(
    settings: Settings, db, vocab: VocabularyProvider | None = None
) -> DedupSweepService:
    """Construct a standalone dedup sweep for the CLI (``python -m app.cli dedup-sweep``). DB-only
    (candidate reads + review-queue writes, no store git); ``vocab`` is the effective-vocabulary
    provider (seeds ∪ approved additions), ``None`` ⇒ seeds-only. The pipeline run-now (M6 task 8)
    passes a real provider so it sweeps the same content types as the in-app nightly."""
    from ..services.agent_runs import PgAgentRunStore
    from ..services.review_queue import PgReviewQueue
    from .store import PgDedupStore

    return DedupSweepService(
        settings=settings,
        dedup_store=PgDedupStore(db),
        review_queue=PgReviewQueue(db),
        run_store=PgAgentRunStore(db),
        vocab=vocab,
    )
