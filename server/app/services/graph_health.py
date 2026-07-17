"""The nightly ``graph-health`` reporter (M8 task 4, ADR-053 §9 / ADR-033 #5).

A **read-only** health check over the settled graph: it runs seven cheap checks, writes the findings
to its own ``agent_runs`` **details** JSON (the ops-console card reads the *latest* ``graph-health``
run — no new table, ADR-053 §9), and stops there. It **never** mutates the graph, never files a
review item, never drains ``inbox/`` — acting on a flag is M10's reflection agent / a manual op
(auto-vs-manual remediation is revisitable then). So it is trivially idempotent (rule 6) and a flaky
run never costs the night anything downstream — it is the nightly **tail** step, ``on_fail:
continue`` (04-pipelines §Scheduling).

The seven checks (ADR-033 #5):
  1. **orphan-nodes** — live, non-``inbox/`` nodes with no *canonical* edge either direction (a
     derived ``similar`` edge is not an asserted relationship, so it doesn't rescue an orphan);
  2. **inbox-depth** — nodes still materialized under ``inbox/`` (organizer-fallback backlog);
  3. **pending-review-aging** — still-decidable (``pending``/``maybe``) review items older than
     ``graph_health_review_aging_days``;
  4. **memories-missing-occurred** — ``memory`` nodes with no ``occurred`` event date;
  5. **alias-less-entities** — entity-hub nodes carrying no ``aliases`` (the resolver's match key);
  6. **tombstone-integrity** — tombstones whose ``merged_into`` points at a now-missing survivor;
  7. **stale-observations** — entity profiles whose newest ``(as of …)`` stamp is older than
     ``graph_health_freshness_days`` (the one more-than-a-count check: it parses the stamps).

Thresholds (review-aging days, freshness window, sample-offender count) are config knobs (rule 9).
Every check reports a **count** plus a bounded **sample** of offender ids/labels so the card is
actionable without loading the whole graph. The job depends on the narrow :class:`GraphHealthStore`
protocol + :class:`~app.services.agent_runs.AgentRunStore`, so it unit-tests against fakes (no live
DB/LLM — 08 testing policy); :class:`PgGraphHealthStore` is the plain-SQL implementation (rule 5).
Emits ``app.*`` INFO progress lines so the M8 live-log handler (ADR-053 §1) captures them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from ..config import Settings
from ..db import Database
from .agent_runs import FAILED, SUCCEEDED, AgentRunStore
from .review_queue import DECIDABLE_STATUSES

logger = logging.getLogger(__name__)

# agent_runs.agent name for the reporter (the visible activity-feed row + the console card key).
AGENT = "graph-health"

# Stable check names — the keys the ops-console graph-health card renders each finding under.
CHECK_ORPHAN_NODES = "orphan-nodes"
CHECK_INBOX_DEPTH = "inbox-depth"
CHECK_REVIEW_AGING = "pending-review-aging"
CHECK_MISSING_OCCURRED = "memories-missing-occurred"
CHECK_ALIAS_LESS = "alias-less-entities"
CHECK_TOMBSTONE_INTEGRITY = "tombstone-integrity"
CHECK_STALE_OBSERVATIONS = "stale-observations"


# --- Store return shapes ------------------------------------------------------------------------


@dataclass(frozen=True)
class Offender:
    """One flagged node/item: its id + a short human label (title, path, or a relation)."""

    id: str
    label: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


@dataclass(frozen=True)
class CountSample:
    """A check's total count + a bounded sample of offenders (the common count-check shape)."""

    count: int
    offenders: list[Offender] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewAgingRaw:
    """The pending-review-aging aggregate: how many items are still decidable, how many are older
    than the aging cutoff, the oldest filed time, and a sample of the aged items."""

    decidable: int
    aged: int
    oldest_created_at: datetime | None
    offenders: list[Offender] = field(default_factory=list)


@dataclass(frozen=True)
class ProfileObservations:
    """One entity profile's observations, for the freshness parse (the ``(as of …)`` stamps live in
    each observation's ``since`` date — ADR-034)."""

    node_id: str
    title: str | None
    observations: list[dict[str, Any]] = field(default_factory=list)


class GraphHealthStore(Protocol):
    """The narrow read surface the reporter runs its checks over (all read-only)."""

    async def orphan_nodes(self, *, inbox_prefix: str, sample: int) -> CountSample: ...

    async def inbox_depth(self, *, inbox_prefix: str, sample: int) -> CountSample: ...

    async def pending_review_aging(
        self, *, decidable: list[str], cutoff: datetime, sample: int
    ) -> ReviewAgingRaw: ...

    async def memories_missing_occurred(self, *, sample: int) -> CountSample: ...

    async def alias_less_entities(self, *, entity_types: list[str], sample: int) -> CountSample: ...

    async def dangling_tombstones(self, *, sample: int) -> CountSample: ...

    async def entity_profiles(self, *, entity_types: list[str]) -> list[ProfileObservations]: ...


# --- Findings + outcome -------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One check's result folded into the run details: the check name, its primary ``count`` (the
    flag magnitude), an optional ``detail`` (extra fields such as thresholds), and the offender
    sample."""

    check: str
    count: int
    offenders: list[Offender] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        return self.count > 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "check": self.check,
            "count": self.count,
            "sample": [o.as_dict() for o in self.offenders],
        }
        out.update(self.detail)
        return out


@dataclass
class GraphHealthOutcome:
    """One reporter pass — feeds the ``graph-health`` agent_runs row + tests."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def flagged_checks(self) -> int:
        return sum(1 for f in self.findings if f.flagged)

    def summary(self) -> str:
        counts = ", ".join(f"{f.check}={f.count}" for f in self.findings)
        return (
            f"graph health: {self.flagged_checks}/{len(self.findings)} check(s) flagged — {counts}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "flagged_checks": self.flagged_checks,
            "checks": [f.as_dict() for f in self.findings],
        }


# --- Pure freshness logic (unit-tested without I/O) ---------------------------------------------


def newest_as_of(observations: list[dict[str, Any]]) -> date | None:
    """The most-recent ``(as of …)`` stamp across an entity profile's observations, or ``None`` when
    none carry a date (freshness can't be judged, so the profile isn't flagged). The stamp is the
    observation's ``since`` date (ADR-034); a malformed value is skipped, never raised."""
    newest: date | None = None
    for obs in observations:
        raw = obs.get("since")
        if not raw:
            continue
        try:
            parsed = date.fromisoformat(str(raw))
        except ValueError:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def stale_entity_profiles(
    profiles: list[ProfileObservations], *, cutoff: date, sample: int
) -> CountSample:
    """The entity profiles whose newest ``(as of …)`` stamp predates ``cutoff`` — the freshness
    check's more-than-a-count leg. Profiles with no dated observation are never flagged (unknown
    freshness). Offenders are ordered oldest-stamp-first and bounded to ``sample``."""
    stale: list[tuple[date, ProfileObservations]] = []
    for profile in profiles:
        newest = newest_as_of(profile.observations)
        if newest is not None and newest < cutoff:
            stale.append((newest, profile))
    stale.sort(key=lambda pair: pair[0])
    offenders = [
        Offender(id=p.node_id, label=f"{p.title or p.node_id} (as of {stamp.isoformat()})")
        for stamp, p in stale[:sample]
    ]
    return CountSample(count=len(stale), offenders=offenders)


# --- The reporter -------------------------------------------------------------------------------


class GraphHealthService:
    """Owns the nightly-tail read-only graph-health report (ADR-053 §9)."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: GraphHealthStore,
        run_store: AgentRunStore,
    ) -> None:
        self._settings = settings
        self._store = store
        self._runs = run_store

    async def run_scheduled(self) -> GraphHealthOutcome | None:
        """The scheduler/CLI/manual-trigger entry point. Opens the run, runs the checks, closes it;
        never raises (rule 7). A run is opened even when everything is clean — the empty report is
        itself the heartbeat the console card reads. Returns the outcome for CLI logging, or
        ``None`` when the run couldn't be opened/failed (the scheduler ignores the return)."""
        try:
            run_id = await self._runs.start(AGENT)
        except Exception:  # noqa: BLE001 — DB down at row-open: log, never crash the job
            logger.exception("could not open agent_runs row for graph-health; skipped")
            return None
        try:
            outcome = await self._collect()
            logger.info("%s", outcome.summary())
            await self._runs.finish(
                run_id, status=SUCCEEDED, summary=outcome.summary(), details=outcome.as_dict()
            )
            return outcome
        except Exception as exc:  # noqa: BLE001 — end the run failed with context, never crash
            logger.exception("graph-health failed")
            await self._safe_finish(run_id, exc)
            return None

    async def _collect(self) -> GraphHealthOutcome:
        settings = self._settings
        sample = settings.graph_health_sample_offenders
        inbox_prefix = f"{settings.inbox_folder}/%"
        entity_types = list(settings.entity_like_types)
        now = datetime.now(UTC)

        logger.info("graph-health: running %s checks", 7)

        orphans = await self._store.orphan_nodes(inbox_prefix=inbox_prefix, sample=sample)
        inbox = await self._store.inbox_depth(inbox_prefix=inbox_prefix, sample=sample)
        aging = await self._review_aging(now=now, sample=sample)
        missing = await self._store.memories_missing_occurred(sample=sample)
        alias_less = await self._store.alias_less_entities(entity_types=entity_types, sample=sample)
        tombstones = await self._store.dangling_tombstones(sample=sample)
        stale = await self._stale_observations(entity_types=entity_types, now=now, sample=sample)

        findings = [
            Finding(CHECK_ORPHAN_NODES, orphans.count, orphans.offenders),
            Finding(CHECK_INBOX_DEPTH, inbox.count, inbox.offenders),
            aging,
            Finding(CHECK_MISSING_OCCURRED, missing.count, missing.offenders),
            Finding(CHECK_ALIAS_LESS, alias_less.count, alias_less.offenders),
            Finding(CHECK_TOMBSTONE_INTEGRITY, tombstones.count, tombstones.offenders),
            stale,
        ]
        return GraphHealthOutcome(findings=findings)

    async def _review_aging(self, *, now: datetime, sample: int) -> Finding:
        threshold_days = self._settings.graph_health_review_aging_days
        cutoff = now - timedelta(days=threshold_days)
        raw = await self._store.pending_review_aging(
            decidable=list(DECIDABLE_STATUSES), cutoff=cutoff, sample=sample
        )
        oldest_age_days = _age_days(raw.oldest_created_at, now) if raw.oldest_created_at else None
        return Finding(
            check=CHECK_REVIEW_AGING,
            count=raw.aged,
            offenders=raw.offenders,
            detail={
                "decidable": raw.decidable,
                "aging_threshold_days": threshold_days,
                "oldest_age_days": oldest_age_days,
            },
        )

    async def _stale_observations(
        self, *, entity_types: list[str], now: datetime, sample: int
    ) -> Finding:
        threshold_days = self._settings.graph_health_freshness_days
        cutoff = (now - timedelta(days=threshold_days)).date()
        profiles = await self._store.entity_profiles(entity_types=entity_types)
        result = stale_entity_profiles(profiles, cutoff=cutoff, sample=sample)
        return Finding(
            check=CHECK_STALE_OBSERVATIONS,
            count=result.count,
            offenders=result.offenders,
            detail={"freshness_threshold_days": threshold_days},
        )

    async def _safe_finish(self, run_id: str, exc: Exception) -> None:
        try:
            await self._runs.finish(
                run_id,
                status=FAILED,
                summary="graph-health failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — last-ditch; the DB may be down
            logger.exception("could not close graph-health agent_runs row %s", run_id)


def _age_days(when: datetime, now: datetime) -> int:
    """Whole-day age of ``when`` relative to ``now``, floored at 0 (never negative on clock skew).
    A naive stamp is read as UTC so an aware/naive subtraction never raises."""
    aware = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return max((now - aware).days, 0)


# --- asyncpg implementation (plain SQL, no ORM — rule 5) ----------------------------------------


def _decode_observations(value: Any) -> list[dict[str, Any]]:
    """Decode the ``node_profiles.observations`` jsonb (asyncpg hands jsonb back as text)."""
    if value is None:
        return []
    if isinstance(value, str):
        loaded = json.loads(value)
    else:
        loaded = value
    return list(loaded) if isinstance(loaded, list) else []


class PgGraphHealthStore:
    """asyncpg-backed graph-health reads — plain SQL, no ORM (ADR-011). Every check excludes
    tombstones (``merged_into`` set) except the tombstone-integrity check, which is *about* them."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def orphan_nodes(self, *, inbox_prefix: str, sample: int) -> CountSample:
        # Live, non-`inbox/` nodes with no canonical edge either direction. Derived `similar` edges
        # are not asserted relationships, so origin='canonical' — a node reachable only by
        # similarity is still a graph orphan (ADR-053 §9). `inbox/` fallbacks are expected
        # edge-less, so they belong to the inbox-depth check, not here (no double-counting).
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH matched AS (
                    SELECT n.id, n.title, n.store_path, n.indexed_at
                      FROM nodes n
                     WHERE n.merged_into IS NULL
                       AND n.store_path NOT LIKE $1
                       AND NOT EXISTS (
                           SELECT 1 FROM edges e
                            WHERE (e.src_id = n.id OR e.dst_id = n.id) AND e.origin = 'canonical'
                       )
                ),
                sample AS (
                    SELECT id, title, store_path FROM matched ORDER BY indexed_at DESC, id LIMIT $2
                )
                SELECT c.total, s.id, s.title, s.store_path
                  FROM (SELECT count(*) AS total FROM matched) c
                  LEFT JOIN sample s ON true
                """,
                inbox_prefix,
                sample,
            )
        return _count_sample(rows, label_key="title", fallback_key="store_path")

    async def inbox_depth(self, *, inbox_prefix: str, sample: int) -> CountSample:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH matched AS (
                    SELECT n.id, n.title, n.store_path, n.indexed_at
                      FROM nodes n
                     WHERE n.merged_into IS NULL
                       AND n.store_path LIKE $1
                ),
                sample AS (
                    SELECT id, title, store_path FROM matched ORDER BY indexed_at DESC, id LIMIT $2
                )
                SELECT c.total, s.id, s.title, s.store_path
                  FROM (SELECT count(*) AS total FROM matched) c
                  LEFT JOIN sample s ON true
                """,
                inbox_prefix,
                sample,
            )
        return _count_sample(rows, label_key="store_path", fallback_key="id")

    async def pending_review_aging(
        self, *, decidable: list[str], cutoff: datetime, sample: int
    ) -> ReviewAgingRaw:
        async with self._db.acquire() as conn:
            agg = await conn.fetchrow(
                """
                SELECT count(*) AS decidable,
                       count(*) FILTER (WHERE created_at < $2) AS aged,
                       min(created_at) AS oldest
                  FROM review_queue
                 WHERE status = ANY($1::text[])
                """,
                decidable,
                cutoff,
            )
            offenders = await conn.fetch(
                """
                SELECT id, kind, created_at
                  FROM review_queue
                 WHERE status = ANY($1::text[]) AND created_at < $2
                 ORDER BY created_at ASC
                 LIMIT $3
                """,
                decidable,
                cutoff,
                sample,
            )
        return ReviewAgingRaw(
            decidable=agg["decidable"] or 0,
            aged=agg["aged"] or 0,
            oldest_created_at=agg["oldest"],
            offenders=[
                Offender(
                    id=str(r["id"]),
                    label=f"{r['kind']} ({r['created_at'].date().isoformat()})",
                )
                for r in offenders
            ],
        )

    async def memories_missing_occurred(self, *, sample: int) -> CountSample:
        # DELIBERATE overlap with inbox-depth: an `inbox/` fallback node is type=memory and usually
        # carries no `occurred`, so it is counted by BOTH checks. That is intentional — the two
        # checks measure distinct health dimensions (unorganized backlog vs. an event date missing
        # on a memory), and a node legitimately failing both should show in both (no de-duping).
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH matched AS (
                    SELECT n.id, n.title, n.store_path, n.node_created_at
                      FROM nodes n
                     WHERE n.merged_into IS NULL
                       AND n.type = 'memory'
                       AND n.occurred_start IS NULL
                ),
                sample AS (
                    SELECT id, title, store_path FROM matched
                     ORDER BY node_created_at DESC NULLS LAST, id LIMIT $1
                )
                SELECT c.total, s.id, s.title, s.store_path
                  FROM (SELECT count(*) AS total FROM matched) c
                  LEFT JOIN sample s ON true
                """,
                sample,
            )
        return _count_sample(rows, label_key="title", fallback_key="store_path")

    async def alias_less_entities(self, *, entity_types: list[str], sample: int) -> CountSample:
        if not entity_types:
            return CountSample(count=0)
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH matched AS (
                    SELECT n.id, n.title, n.store_path, n.indexed_at
                      FROM nodes n
                     WHERE n.merged_into IS NULL
                       AND n.type = ANY($1::text[])
                       AND (n.aliases IS NULL OR cardinality(n.aliases) = 0)
                ),
                sample AS (
                    SELECT id, title, store_path FROM matched ORDER BY indexed_at DESC, id LIMIT $2
                )
                SELECT c.total, s.id, s.title, s.store_path
                  FROM (SELECT count(*) AS total FROM matched) c
                  LEFT JOIN sample s ON true
                """,
                entity_types,
                sample,
            )
        return _count_sample(rows, label_key="title", fallback_key="store_path")

    async def dangling_tombstones(self, *, sample: int) -> CountSample:
        # Tombstones whose survivor no longer resolves — the only check that is *about* merged_into
        # rows, so it does not exclude them. A dangling merged_into breaks id-resolution after a
        # merge (ADR-030 §5), so it is a genuine integrity flag.
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH matched AS (
                    SELECT n.id, n.merged_into
                      FROM nodes n
                     WHERE n.merged_into IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM nodes t WHERE t.id = n.merged_into)
                ),
                sample AS (
                    SELECT id, merged_into FROM matched ORDER BY id LIMIT $1
                )
                SELECT c.total, s.id, s.merged_into
                  FROM (SELECT count(*) AS total FROM matched) c
                  LEFT JOIN sample s ON true
                """,
                sample,
            )
        count = rows[0]["total"] if rows else 0
        offenders = [
            Offender(id=str(r["id"]), label=f"{r['id']} -> {r['merged_into']} (missing)")
            for r in rows
            if r["id"] is not None
        ]
        return CountSample(count=count, offenders=offenders)

    async def entity_profiles(self, *, entity_types: list[str]) -> list[ProfileObservations]:
        # One row per live entity hub that has a derived profile; the freshness parse reads the
        # `(as of …)` stamps out of each profile's observations in pure code (ADR-053 §9). Bounded
        # by the entity count (each profile's observations are already capped at
        # PROFILE_MAX_OBSERVATIONS by profile-refresh), so this stays a personal-scale read.
        if not entity_types:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT n.id, n.title, p.observations
                  FROM node_profiles p
                  JOIN nodes n ON n.id = p.node_id
                 WHERE n.merged_into IS NULL
                   AND n.type = ANY($1::text[])
                """,
                entity_types,
            )
        return [
            ProfileObservations(
                node_id=str(r["id"]),
                title=r["title"],
                observations=_decode_observations(r["observations"]),
            )
            for r in rows
        ]


def _count_sample(rows: list[Any], *, label_key: str, fallback_key: str) -> CountSample:
    """Fold a ``count(*) …LEFT JOIN sample`` result set into a :class:`CountSample`. The **total is
    decoupled from the sample** (a ``count(*)`` over the full match set) so any ``sample`` — 0
    included — still reports the true count (rule 7: no silent cap; a ``LIMIT 0`` must never zero
    the count). The count sits on every returned row; when the sample is empty the single row
    carries a NULL id, which is skipped so no phantom offender is emitted."""
    count = (rows[0]["total"] if rows else 0) or 0
    offenders = [
        Offender(id=str(r["id"]), label=str(r[label_key] or r[fallback_key]))
        for r in rows
        if r["id"] is not None
    ]
    return CountSample(count=count, offenders=offenders)


def build_graph_health_service(settings: Settings, db: Database) -> GraphHealthService:
    """Construct a standalone graph-health reporter for the nightly-tail pipeline step + the manual
    ``POST /agents/graph-health/run`` trigger (ADR-053 §8). DB-only (read-only checks + its own run
    row, no store git) — like the maybe-digest / dedup-sweep reporters."""
    from .agent_runs import PgAgentRunStore

    return GraphHealthService(
        settings=settings,
        store=PgGraphHealthStore(db),
        run_store=PgAgentRunStore(db),
    )
