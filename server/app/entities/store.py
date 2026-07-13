"""The alias index — candidate lookup for entity resolution (02-data-model §3, ADR-030 §1).

The DB alias index *is* the GIN over ``nodes.aliases`` (migration 005); this store exposes the one
read the resolver needs: given a mention's surface form + the entity-like types, the **existing
candidate nodes** whose aliases (or title) match. Matching is exact on the *normalized* form
(lower-cased, whitespace-collapsed): any surface form already recorded on an entity's ``aliases``
resolves to that hub, but an *un-recorded* variant (``Alexandru`` when only ``Alex`` is stored)
does not yet collapse. **Two pieces are a documented M3 follow-up** (see 08-logs/m3.md): fuzzy
(trigram) matching — migration 005 does not enable ``pg_trgm``, and ADR-032's entropy guard means
short aliases must never fuzzy-link anyway — and *alias accretion* (recording a newly-met surface
form onto the matched entity so it resolves next time).

The resolver depends on the :class:`AliasStore` *protocol*, so it unit-tests against an in-memory
fake (no live DB in CI). :class:`PgAliasStore` is the plain-SQL asyncpg implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..db import Database


def normalize_alias(name: str) -> str:
    """The match key for an alias/mention: lower-cased, whitespace-collapsed."""
    return " ".join(name.lower().split())


@dataclass(frozen=True)
class EntityCandidate:
    """An existing entity node that matched a mention — the structured fields injected into the
    resolver prompt (ADR-030 §2: never node bodies, only id/name/aliases/disambig/type)."""

    id: str
    type: str
    title: str | None
    aliases: list[str] = field(default_factory=list)
    disambig: str | None = None


class AliasStore(Protocol):
    """The candidate-lookup surface the resolver relies on."""

    async def find_candidates(self, name: str, *, types: list[str]) -> list[EntityCandidate]:
        """Existing non-tombstone entity nodes (of ``types``) whose normalized aliases/title match
        ``name``. Bounded by construction — one mention's candidates, never the whole registry."""
        ...


class PgAliasStore:
    """asyncpg-backed alias index — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def find_candidates(self, name: str, *, types: list[str]) -> list[EntityCandidate]:
        key = normalize_alias(name)
        if not key or not types:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, type, title, aliases, disambig
                FROM nodes
                WHERE type = ANY($2::text[])
                  AND merged_into IS NULL
                  AND (
                        lower(title) = $1
                     OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = $1)
                  )
                ORDER BY title
                """,
                key,
                types,
            )
        return [
            EntityCandidate(
                id=str(r["id"]),
                type=r["type"],
                title=r["title"],
                aliases=list(r["aliases"] or []),
                disambig=r["disambig"],
            )
            for r in rows
        ]
