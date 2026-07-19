"""Durable entity-merge decisions — the replayable record behind ADR-064 §1 (02-data-model §3).

A manual entity merge (ADR-030 §5) writes a tombstone keyed on the loser's **node id**;
``reprocess-all`` mints fresh ids from raw, so that tombstone can't be re-applied by id and the
merge is silently dropped (ADR-042 §4 could only *warn*). This store records each merge as a durable
decision keyed on **stable identity — the loser's normalized surface forms (name + aliases) + node
type — not its id** — so the reprocess replay (``app.entities.merge_replay.MergeReplayService``)
re-folds any re-created hub matching a recorded loser back into its survivor.

Plain SQL over asyncpg (rule 5, ADR-011); callers depend on the :class:`MergeDecisionStore` protocol
so they unit-test against a fake. :class:`PgMergeDecisionStore` is the implementation; ``record`` is
an upsert on the loser identity key (idempotent — re-applying the same merge updates the one row).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from ..db import Database
from .store import normalize_alias


def surface_forms(title: str | None, aliases: Iterable[str]) -> list[str]:
    """A hub's **normalized** surface forms — its title + aliases, folded/lower-cased/collapsed via
    :func:`normalize_alias` (ADR-041), deduped, **title first**. The title form is the strong
    identity the replay lookup ranks on; the alias forms widen the match so a re-created hub is
    found even under a variant surface form (ADR-064 §1)."""
    forms: list[str] = []
    for raw in [title, *aliases]:
        if not raw:
            continue
        norm = normalize_alias(raw)
        if norm and norm not in forms:
            forms.append(norm)
    return forms


def loser_key(node_type: str, forms: Iterable[str]) -> str:
    """The idempotency key for a durable decision: the loser's type + its sorted normalized forms.
    Deterministic across re-applies of the same merge, so the store upserts one row (ADR-064 §1).
    The separators (NUL / SOH) can't occur in a normalized surface form, so the join is unambiguous.
    """
    return node_type + "\x00" + "\x01".join(sorted(forms))


@dataclass(frozen=True)
class MergeDecision:
    """One durable merge decision — the survivor + loser identities to replay (ADR-064 §1).

    ``survivor_forms``/``loser_forms`` are normalized (via :func:`surface_forms`) and captured at
    merge time *before* the alias union, so a reprocessed rebuild (survivor & loser each re-created
    from raw with their own forms) resolves both sides distinctly. ``*_node_id`` are the merge-time
    ids, kept for observability only (they no longer resolve after a reprocess)."""

    survivor_type: str
    survivor_forms: list[str]
    loser_type: str
    loser_forms: list[str]
    survivor_node_id: str | None = None
    loser_node_id: str | None = None

    @property
    def key(self) -> str:
        return loser_key(self.loser_type, self.loser_forms)


class MergeDecisionStore(Protocol):
    """The durable-merge read/write surface the merge + reprocess-replay services share."""

    async def record(self, decision: MergeDecision) -> None:
        """Persist (upsert on the loser identity key) one merge decision (ADR-064 §1)."""
        ...

    async def all_decisions(self) -> list[MergeDecision]:
        """Every recorded decision, oldest first — the replay order (earlier merges re-apply
        first, so a chained merge lands deterministically)."""
        ...

    async def count(self) -> int:
        """How many durable decisions exist — the reprocess preview's "will be re-applied" count."""
        ...


class PgMergeDecisionStore:
    """asyncpg-backed durable-merge store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, decision: MergeDecision) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO entity_merges (
                    id, survivor_type, survivor_forms, loser_type, loser_forms, loser_key,
                    survivor_node_id, loser_node_id
                ) VALUES ($1, $2, $3::text[], $4, $5::text[], $6, $7, $8)
                ON CONFLICT (loser_key) DO UPDATE SET
                    survivor_type    = EXCLUDED.survivor_type,
                    survivor_forms   = EXCLUDED.survivor_forms,
                    loser_type       = EXCLUDED.loser_type,
                    loser_forms      = EXCLUDED.loser_forms,
                    survivor_node_id = EXCLUDED.survivor_node_id,
                    loser_node_id    = EXCLUDED.loser_node_id,
                    created_at       = now()
                """,
                str(uuid.uuid4()),
                decision.survivor_type,
                decision.survivor_forms,
                decision.loser_type,
                decision.loser_forms,
                decision.key,
                decision.survivor_node_id,
                decision.loser_node_id,
            )

    async def all_decisions(self) -> list[MergeDecision]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT survivor_type, survivor_forms, loser_type, loser_forms,
                       survivor_node_id, loser_node_id
                FROM entity_merges
                ORDER BY created_at ASC, loser_key ASC
                """
            )
        return [
            MergeDecision(
                survivor_type=r["survivor_type"],
                survivor_forms=list(r["survivor_forms"] or []),
                loser_type=r["loser_type"],
                loser_forms=list(r["loser_forms"] or []),
                survivor_node_id=r["survivor_node_id"],
                loser_node_id=r["loser_node_id"],
            )
            for r in rows
        ]

    async def count(self) -> int:
        async with self._db.acquire() as conn:
            value = await conn.fetchval("SELECT count(*) FROM entity_merges")
        return int(value or 0)
