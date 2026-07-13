"""Persistence for tag-vocabulary reuse + consolidation (02-data-model §2/§3, ADR-024).

Both features read from the derived ``notes`` index (M2): the live tag vocabulary is the
distinct ``notes.tags`` aggregation, and consolidation locates the notes that carry a given set
of variant tags. As elsewhere, the callers depend on the :class:`TagStore` *protocol* so they
unit-test against an in-memory fake (no live DB in CI — 08 testing policy); :class:`PgTagStore`
is the plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011).

The vocabulary is a *cache of the vault* (rule 1): a freshly-wiped index yields an empty
vocabulary, which the organizer simply treats as "no existing tags yet" — never an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class TagCount:
    """One distinct vault tag and how many notes carry it (frequency)."""

    tag: str
    count: int


@dataclass(frozen=True)
class TaggedNote:
    """A note that carries at least one of the queried tags — the unit of a consolidation apply."""

    vault_path: str


class TagVocabulary(Protocol):
    """The narrow read the organizer path needs: the current tag vocabulary as plain strings."""

    async def vocabulary_tags(self, *, limit: int) -> list[str]:
        """The ``limit`` most-used distinct tags, most-used first (ADR-024 §1)."""
        ...


class TagStore(TagVocabulary, Protocol):
    """The full tag-persistence surface (vocabulary + consolidation lookups)."""

    async def tag_counts(self, *, limit: int) -> list[TagCount]:
        """The ``limit`` most-used distinct tags with their note frequency, most-used first."""
        ...

    async def notes_with_any_tag(self, tags: list[str]) -> list[TaggedNote]:
        """Every indexed note whose ``tags`` overlap the given set (consolidation apply target)."""
        ...


class PgTagStore:
    """asyncpg-backed tag store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def tag_counts(self, *, limit: int) -> list[TagCount]:
        if limit <= 0:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tag, count(*) AS n
                FROM notes, unnest(tags) AS tag
                GROUP BY tag
                ORDER BY n DESC, tag
                LIMIT $1
                """,
                limit,
            )
        return [TagCount(tag=row["tag"], count=int(row["n"])) for row in rows]

    async def vocabulary_tags(self, *, limit: int) -> list[str]:
        return [tc.tag for tc in await self.tag_counts(limit=limit)]

    async def notes_with_any_tag(self, tags: list[str]) -> list[TaggedNote]:
        if not tags:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT vault_path FROM notes WHERE tags && $1::text[] ORDER BY vault_path",
                list(tags),
            )
        return [TaggedNote(vault_path=row["vault_path"]) for row in rows]
