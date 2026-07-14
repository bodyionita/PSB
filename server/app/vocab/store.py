"""Approved-vocabulary persistence (ADR-027 §Consequences / ADR-035, M3 task 7).

The starter node/edge vocabularies are config seeds (:class:`~app.config.Settings`); the
**approved additions** — the types the user accepted through governance — live in ``app_settings``
under one key (02-data-model §3 "approved vocabulary lives in config + ``app_settings``"). The
*effective* vocabulary a writer sees is seeds ∪ these additions (composed in
:mod:`app.vocab.service`); this module only stores and reads the additions.

One jsonb value keeps the three axes together::

    {"node_types": [...], "edge_rels": [...], "entity_like_types": [...]}

Plain SQL over asyncpg, no ORM (rule 5, ADR-011). The service depends on the
:class:`VocabularyStore` protocol so it unit-tests against an in-memory fake (no live DB — 08
testing policy).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from ..db import Database

# The single ``app_settings`` key holding the approved vocabulary additions (all three axes).
VOCABULARY_KEY = "vocabulary"

# The axes an approval can extend. ``entity_like_types`` is always also a ``node_types`` member
# (an entity-like type is a node type that carries the entity substrate — ADR-030), so approving
# an entity type extends both; :class:`VocabularyService` owns that mapping.
AXES = ("node_types", "edge_rels", "entity_like_types")


@dataclass(frozen=True)
class VocabularyAdditions:
    """The user-approved vocabulary additions per axis (each axis's *extra* beyond the seeds)."""

    node_types: tuple[str, ...] = ()
    edge_rels: tuple[str, ...] = ()
    entity_like_types: tuple[str, ...] = ()


class VocabularyStore(Protocol):
    """Read/extend the approved-vocabulary additions (the mutable half of the vocabulary)."""

    async def get_additions(self) -> VocabularyAdditions:
        """The approved additions per axis (empty tuples when nothing has been approved)."""
        ...

    async def add(
        self,
        *,
        node_types: tuple[str, ...] | list[str] = (),
        edge_rels: tuple[str, ...] | list[str] = (),
        entity_like_types: tuple[str, ...] | list[str] = (),
    ) -> VocabularyAdditions:
        """Append the given values to their axes (dedup, order-preserving); returns the new set.

        Idempotent: a value already present is a no-op, so re-approving the same type never
        duplicates it. Returns the full additions after the write."""
        ...


class PgVocabularyStore:
    """asyncpg-backed approved-vocabulary store over ``app_settings`` — plain SQL (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_additions(self) -> VocabularyAdditions:
        async with self._db.acquire() as conn:
            value = await conn.fetchval(
                "SELECT value FROM app_settings WHERE key = $1", VOCABULARY_KEY
            )
        return _decode(value)

    async def add(
        self,
        *,
        node_types: tuple[str, ...] | list[str] = (),
        edge_rels: tuple[str, ...] | list[str] = (),
        entity_like_types: tuple[str, ...] | list[str] = (),
    ) -> VocabularyAdditions:
        incoming = {
            "node_types": list(node_types),
            "edge_rels": list(edge_rels),
            "entity_like_types": list(entity_like_types),
        }
        # Read-modify-write in one transaction (single-user; app_settings is low-contention). The
        # row may not exist yet, so upsert with ON CONFLICT after computing the merged value.
        # NOTE: on the very first approval the row is absent, so ``FOR UPDATE`` locks nothing and
        # two concurrent first-approvals could each merge from empty (a lost update). Harmless for a
        # single user approving one at a time; once the row exists ``FOR UPDATE`` serializes writes.
        async with self._db.transaction() as conn:
            current = _decode(
                await conn.fetchval(
                    "SELECT value FROM app_settings WHERE key = $1 FOR UPDATE", VOCABULARY_KEY
                )
            )
            merged = {
                "node_types": _merge(current.node_types, incoming["node_types"]),
                "edge_rels": _merge(current.edge_rels, incoming["edge_rels"]),
                "entity_like_types": _merge(
                    current.entity_like_types, incoming["entity_like_types"]
                ),
            }
            await conn.execute(
                """
                INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = $2::jsonb, updated_at = now()
                """,
                VOCABULARY_KEY,
                json.dumps(merged),
            )
        return VocabularyAdditions(
            node_types=tuple(merged["node_types"]),
            edge_rels=tuple(merged["edge_rels"]),
            entity_like_types=tuple(merged["entity_like_types"]),
        )


def _merge(existing: tuple[str, ...], incoming: list[str]) -> list[str]:
    """Append ``incoming`` to ``existing``, dropping empties + duplicates, preserving order."""
    out = list(existing)
    for value in incoming:
        v = value.strip()
        if v and v not in out:
            out.append(v)
    return out


def _decode(value: Any) -> VocabularyAdditions:
    """Decode the jsonb column (asyncpg returns jsonb as text) into :class:`VocabularyAdditions`."""
    if value is None:
        return VocabularyAdditions()
    obj = json.loads(value) if isinstance(value, str) else dict(value)
    if not isinstance(obj, dict):
        return VocabularyAdditions()
    return VocabularyAdditions(
        node_types=_clean(obj.get("node_types")),
        edge_rels=_clean(obj.get("edge_rels")),
        entity_like_types=_clean(obj.get("entity_like_types")),
    )


def _clean(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return tuple(out)
