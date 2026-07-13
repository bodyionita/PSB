"""Read side of the derived index (03-api §Search & notes, 02 §3).

The search service depends on the :class:`SearchStore` *protocol*, not on asyncpg, so it unit-tests
against an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgSearchStore` is the
plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011).

Query embeddings pass as plain ``list[float]`` — the ``vector`` codec on the pool (see ``db.py``)
encodes them for the ``<=>`` cosine operator, which runs against the HNSW index (migration 004).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class SearchHit:
    """One note in a search result — its best-scoring chunk supplies the snippet + score."""

    note_id: str
    vault_path: str
    title: str | None
    plane: str | None
    planes: list[str]
    tags: list[str]
    snippet: str
    score: float


@dataclass(frozen=True)
class RelatedNote:
    """A ``note_links`` neighbour of a note (ADR-023)."""

    note_id: str
    vault_path: str
    title: str | None
    score: float


@dataclass(frozen=True)
class NoteRow:
    """A note's stored metadata + its semantic neighbours (body is read from the vault file)."""

    note_id: str
    vault_path: str
    title: str | None
    plane: str | None
    planes: list[str]
    tags: list[str]
    related: list[RelatedNote] = field(default_factory=list)


class SearchStore(Protocol):
    """The read surface the search service relies on."""

    async def search_chunks(
        self,
        embedding: list[float],
        *,
        top_k: int,
        planes: list[str] | None,
        min_score: float,
    ) -> list[SearchHit]:
        """Note-grouped cosine search: one row per note (best chunk), ranked by score desc."""
        ...

    async def get_note(self, note_id: str) -> NoteRow | None:
        """A note's metadata + ``note_links`` neighbours, or ``None`` if the id is unknown."""
        ...


class PgSearchStore:
    """asyncpg-backed read store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def search_chunks(
        self,
        embedding: list[float],
        *,
        top_k: int,
        planes: list[str] | None,
        min_score: float,
    ) -> list[SearchHit]:
        # Best chunk per note via DISTINCT ON (note, ascending cosine distance), then re-rank the
        # per-note winners by score and take top_k. `planes && $2` is array-overlap membership
        # (ADR-005 — never folder). `$2 IS NULL` skips the filter. score = 1 - cosine distance.
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT note_id, vault_path, title, plane, planes, tags, snippet, score
                FROM (
                    SELECT DISTINCT ON (n.id)
                        n.id          AS note_id,
                        n.vault_path  AS vault_path,
                        n.title       AS title,
                        n.plane       AS plane,
                        n.planes      AS planes,
                        n.tags        AS tags,
                        c.content     AS snippet,
                        1 - (c.embedding <=> $1) AS score
                    FROM chunks c
                    JOIN notes n ON n.id = c.note_id
                    WHERE c.embedding IS NOT NULL
                      AND ($2::text[] IS NULL OR n.planes && $2::text[])
                    ORDER BY n.id, c.embedding <=> $1
                ) best
                WHERE score >= $3
                ORDER BY score DESC
                LIMIT $4
                """,
                embedding,
                planes,
                min_score,
                top_k,
            )
        return [
            SearchHit(
                note_id=str(row["note_id"]),
                vault_path=row["vault_path"],
                title=row["title"],
                plane=row["plane"],
                planes=list(row["planes"] or []),
                tags=list(row["tags"] or []),
                snippet=row["snippet"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def get_note(self, note_id: str) -> NoteRow | None:
        async with self._db.acquire() as conn:
            note = await conn.fetchrow(
                "SELECT id, vault_path, title, plane, planes, tags FROM notes WHERE id = $1",
                note_id,
            )
            if note is None:
                return None
            related = await conn.fetch(
                """
                SELECT nl.related_note_id AS note_id, nl.score AS score,
                       n2.vault_path AS vault_path, n2.title AS title
                FROM note_links nl
                JOIN notes n2 ON n2.id = nl.related_note_id
                WHERE nl.note_id = $1
                ORDER BY nl.score DESC
                """,
                note_id,
            )
        return NoteRow(
            note_id=str(note["id"]),
            vault_path=note["vault_path"],
            title=note["title"],
            plane=note["plane"],
            planes=list(note["planes"] or []),
            tags=list(note["tags"] or []),
            related=[
                RelatedNote(
                    note_id=str(r["note_id"]),
                    vault_path=r["vault_path"],
                    title=r["title"],
                    score=float(r["score"]),
                )
                for r in related
            ],
        )
