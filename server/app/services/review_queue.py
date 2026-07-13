"""The kind-generic review queue (02-data-model §3, ADR-030 §3 / ADR-029).

Every human-decision item the system files goes through one table, one lifecycle
(``pending → resolved/discarded/maybe``). M3 files two kinds — ``entity-ambiguity`` (the
organizer couldn't confidently resolve an entity mention, ADR-030 §3) and ``vocab-proposal``
(the organizer proposed a node/edge type outside the seeded vocabulary, ADR-027). The
``stance-candidate`` (M6) and ``dedup-proposal`` (M6+) kinds reuse the same table later.

This module is the **write path** the organizer needs. The admin *read/resolve* surface (the
minimal Review list + resolution that materializes a pending edge, and vocab-approve →
consolidation) is M3 task 4 — it composes over the same ``review_queue`` table. The caller
depends on the :class:`ReviewQueue` *protocol* so it unit-tests against an in-memory fake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..db import Database

KIND_ENTITY_AMBIGUITY = "entity-ambiguity"
KIND_VOCAB_PROPOSAL = "vocab-proposal"


@dataclass(frozen=True)
class ReviewItem:
    """One item to file. ``payload`` carries the kind-specific data (candidates / proposed type);
    ``excerpt`` is the mention shown in its capture context so the item is decidable in place."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    excerpt: str | None = None
    source: str | None = None
    source_ref: str | None = None


class ReviewQueue(Protocol):
    """The write surface the organizer/entity-resolution path relies on."""

    async def enqueue(self, item: ReviewItem) -> str:
        """File a ``pending`` review item; returns its id."""
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
