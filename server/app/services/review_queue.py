"""The kind-generic review queue (02-data-model §3, ADR-030 §3 / ADR-029).

Every human-decision item the system files goes through one table, one lifecycle: ``pending`` →
``resolved``/``discarded`` (terminal), or ``maybe`` (parked but **still-decidable** — ADR-048 §7,
a maybe re-opens to a later agree/disagree). M3 files two kinds — ``entity-ambiguity`` (the
organizer couldn't confidently resolve an entity mention, ADR-030 §3) and ``vocab-proposal``
(the organizer proposed a node/edge type outside the seeded vocabulary, ADR-027). The
``stance-candidate`` (M6) and ``dedup-proposal`` (M6+) kinds reuse the same table later.

Two surfaces over the one table:
  * :class:`ReviewQueue` — the **write path** the organizer/entity-resolution relies on
    (``enqueue``); the resolver depends on this narrow protocol so it unit-tests against a fake.
  * :class:`ReviewReadStore` — the **read/resolve path** for the admin Review surface (M3 task 4):
    list decidable-in-place items and transition one out of a decidable state
    (``pending``/``maybe``). The *materialization*
    logic (pending edge → file + DB, vocab-approve → consolidation queue-hook) lives in
    ``review_service.py`` (business logic, rule 5); this store only reads rows and flips status.

:class:`PgReviewQueue` implements both (plain SQL, no ORM — rule 5, ADR-011).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..db import Database

KIND_ENTITY_AMBIGUITY = "entity-ambiguity"
KIND_VOCAB_PROPOSAL = "vocab-proposal"
# M6 kinds (ADR-048): a stance-unclear chat-distilled memory awaiting agree/disagree/maybe, and a
# near-duplicate node pair (M6 task 5). `stance-candidate` payloads carry names + text, never node
# ids, so a reprocess that rebuilds the graph can't strand them (ADR-048 §7).
KIND_STANCE_CANDIDATE = "stance-candidate"
KIND_DEDUP_PROPOSAL = "dedup-proposal"

# Lifecycle statuses (ADR-030 §3). Items start ``pending``; ``maybe`` is a **parked, still-
# decidable** state (ADR-048 §7) — it accepts a later agree/disagree — while ``resolved``/
# ``discarded`` are terminal. So the resolve guard is "decidable" (``pending`` ∪ ``maybe``).
STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_DISCARDED = "discarded"
STATUS_MAYBE = "maybe"

# The still-decidable statuses a ``resolve`` may transition out of (ADR-048 §7): ``pending`` and
# the re-openable ``maybe``. ``resolved``/``discarded`` are terminal — a resolve on one is a no-op.
DECIDABLE_STATUSES = (STATUS_PENDING, STATUS_MAYBE)


# Resolution errors, shared by every service that resolves a review item (the Review service for
# entity-ambiguity, the Vocabulary service for vocab-proposals) so the router maps one exception
# set to HTTP status codes regardless of which service handled the kind. They live here — the
# neutral, already-shared module — to keep those services from importing each other.
class ReviewError(Exception):
    """Base for review-resolution problems surfaced to the API layer."""


class ReviewNotFound(ReviewError):
    """No review item with the given id (404)."""


class ReviewNotPending(ReviewError):
    """The item is terminal (already ``resolved``/``discarded``) — it cannot be decided again (409).
    A parked ``maybe`` is NOT terminal (re-openable, ADR-048 §7), so it does not raise this."""


class BadResolution(ReviewError):
    """The resolution body is invalid for the item's kind (400)."""


@dataclass(frozen=True)
class ReviewItem:
    """One item to file. ``payload`` carries the kind-specific data (candidates / proposed type);
    ``excerpt`` is the mention shown in its capture context so the item is decidable in place."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    excerpt: str | None = None
    source: str | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class ReviewRecord:
    """A filed review item as read back for the admin surface (a full ``review_queue`` row)."""

    id: str
    kind: str
    payload: dict[str, Any]
    excerpt: str | None
    source: str | None
    source_ref: str | None
    status: str
    resolution: dict[str, Any] | None
    created_at: datetime


@dataclass(frozen=True)
class MaybeKindStat:
    """Parked-``maybe`` aggregate for the weekly maybe-digest (ADR-048 §8): per-kind count + the
    oldest filed time, so the digest can report totals + aging (an untriaged pile stalls the
    feature) without loading every row. One row per kind that has at least one parked ``maybe``."""

    kind: str
    count: int
    oldest_created_at: datetime


class ReviewQueue(Protocol):
    """The write surface the organizer/entity-resolution path relies on."""

    async def enqueue(self, item: ReviewItem) -> str:
        """File a ``pending`` review item; returns its id."""
        ...


class ReviewReadStore(Protocol):
    """The read/resolve surface the admin Review service composes over (M3 task 4)."""

    async def list_items(
        self, *, status: str | None, kind: str | None, limit: int
    ) -> list[ReviewRecord]:
        """Newest-first review items, optionally filtered by ``status`` and/or ``kind``."""
        ...

    async def get(self, review_id: str) -> ReviewRecord | None:
        """One review item by id, or ``None`` if unknown."""
        ...

    async def resolve(
        self, review_id: str, *, status: str, resolution: dict[str, Any]
    ) -> bool:
        """Transition a still-**decidable** item (``pending`` or the re-openable ``maybe`` — ADR-048
        §7) to ``status`` + record ``resolution``. Guarded on those two, so a decide on a terminal
        (``resolved``/``discarded``) row is a no-op; returns whether a row transitioned."""
        ...


class PgReviewQueue:
    """asyncpg-backed review queue — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def enqueue(self, item: ReviewItem) -> str:
        async with self._db.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO review_queue (kind, payload, excerpt, source, source_ref)
                VALUES ($1, $2::jsonb, $3, $4, $5)
                RETURNING id
                """,
                item.kind,
                json.dumps(item.payload),
                item.excerpt,
                item.source,
                item.source_ref,
            )
        return str(row_id)

    async def list_items(
        self, *, status: str | None, kind: str | None, limit: int
    ) -> list[ReviewRecord]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, kind, payload, excerpt, source, source_ref, status, resolution,
                       created_at
                  FROM review_queue
                 WHERE ($1::text IS NULL OR status = $1)
                   AND ($2::text IS NULL OR kind = $2)
                 ORDER BY created_at DESC
                 LIMIT $3
                """,
                status,
                kind,
                limit,
            )
        return [_row_to_record(row) for row in rows]

    async def get(self, review_id: str) -> ReviewRecord | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, kind, payload, excerpt, source, source_ref, status, resolution,
                       created_at
                  FROM review_queue
                 WHERE id = $1
                """,
                review_id,
            )
        return _row_to_record(row) if row is not None else None

    async def maybe_kind_stats(self) -> list[MaybeKindStat]:
        """Per-kind count + oldest filed time of the parked ``maybe`` items — the weekly
        maybe-digest aggregate (ADR-048 §8). An empty list means nothing is parked. A cheap ``GROUP
        BY`` so the digest never loads every row."""
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT kind, count(*) AS n, min(created_at) AS oldest
                  FROM review_queue
                 WHERE status = $1
                 GROUP BY kind
                 ORDER BY n DESC, kind
                """,
                STATUS_MAYBE,
            )
        return [
            MaybeKindStat(kind=r["kind"], count=r["n"], oldest_created_at=r["oldest"])
            for r in rows
        ]

    async def resolve(
        self, review_id: str, *, status: str, resolution: dict[str, Any]
    ) -> bool:
        async with self._db.acquire() as conn:
            # Decidable = pending ∪ maybe (ADR-048 §7): a parked `maybe` re-opens to agree/disagree.
            # resolved/discarded are terminal — a decide on one matches no row (no-op → 409 above).
            row_id = await conn.fetchval(
                """
                UPDATE review_queue
                   SET status = $2, resolution = $3::jsonb, resolved_at = now()
                 WHERE id = $1 AND status = ANY($4::text[])
                RETURNING id
                """,
                review_id,
                status,
                json.dumps(resolution),
                list(DECIDABLE_STATUSES),
            )
        return row_id is not None


def _json_obj(value: Any) -> dict[str, Any]:
    """Decode a jsonb column (asyncpg returns it as text by default) into a dict; ``{}`` if null."""
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _row_to_record(row: Any) -> ReviewRecord:
    return ReviewRecord(
        id=str(row["id"]),
        kind=row["kind"],
        payload=_json_obj(row["payload"]),
        excerpt=row["excerpt"],
        source=row["source"],
        source_ref=row["source_ref"],
        status=row["status"],
        resolution=_json_obj(row["resolution"]) if row["resolution"] is not None else None,
        created_at=row["created_at"],
    )
