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
from ..text import fold_diacritics


def normalize_alias(name: str) -> str:
    """The match key for an alias/mention: diacritic-folded (ADR-041), lower-cased, whitespace-
    collapsed. Folding here keeps matching consistent with the already-folded stored aliases/titles
    (``NodeWriter`` folds on write), so ``"Madalina"`` and a raw ``"Mădălina"`` compare equal."""
    return " ".join(fold_diacritics(name).lower().split())


@dataclass(frozen=True)
class EntityCandidate:
    """An existing entity node that matched a mention. ``id``/``name``/``aliases``/``disambig``/
    ``type`` are the structured fields injected into the resolver prompt (ADR-030 §2: never node
    bodies); ``store_path`` is carried for **alias accretion** (ADR-040) — the caller rewrites the
    linked hub's ``aliases`` file — and is NOT sent to the LLM."""

    id: str
    type: str
    title: str | None
    aliases: list[str] = field(default_factory=list)
    disambig: str | None = None
    store_path: str | None = None


class AliasStore(Protocol):
    """The candidate-lookup surface the resolver relies on."""

    async def find_candidates(
        self,
        name: str,
        *,
        types: list[str],
        tokens: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EntityCandidate]:
        """Existing non-tombstone entity nodes (of ``types``) that match ``name``. Two legs
        (ADR-030 §1 / ADR-040 §1): the **exact** leg (normalized title/alias equals ``name``) and,
        when ``tokens`` is non-empty, a **token-overlap** leg (a hub whose title/alias shares a
        significant token with the mention — so ``"Horia Fenwick"`` surfaces the ``"Horia"``
        hub). Exact hits rank first; the result is capped at ``limit``. Bounded by construction —
        one mention's candidates, never the whole registry."""
        ...


class PgAliasStore:
    """asyncpg-backed alias index — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def find_candidates(
        self,
        name: str,
        *,
        types: list[str],
        tokens: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EntityCandidate]:
        key = normalize_alias(name)
        if not key or not types:
            return []
        # Significant tokens (folded + lower-cased by the resolver) drive the fuzzy leg; empty ⇒
        # exact-only (the low-entropy guard, ADR-040 §2). Both title and aliases are folded on write
        # so the comparison is diacritic-insensitive without extra work here (ADR-041).
        toks = list(tokens or [])
        lim = limit if limit is not None else 100
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                r"""
                SELECT id, type, title, store_path, aliases, disambig
                FROM nodes
                WHERE type = ANY($2::text[])
                  AND merged_into IS NULL
                  AND (
                        lower(title) = $1
                     OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = $1)
                     OR (
                          cardinality($3::text[]) > 0
                          AND EXISTS (
                              SELECT 1
                              FROM unnest(
                                     CASE WHEN title IS NULL THEN coalesce(aliases, '{}')
                                          ELSE array_append(coalesce(aliases, '{}'), title) END
                                   ) AS surf
                              WHERE regexp_split_to_array(lower(surf), '\s+') && $3::text[]
                          )
                     )
                  )
                ORDER BY
                  (lower(title) = $1
                   OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = $1)) DESC,
                  title
                LIMIT $4
                """,
                key,
                types,
                toks,
                lim,
            )
        return [
            EntityCandidate(
                id=str(r["id"]),
                type=r["type"],
                title=r["title"],
                aliases=list(r["aliases"] or []),
                disambig=r["disambig"],
                store_path=r["store_path"],
            )
            for r in rows
        ]
