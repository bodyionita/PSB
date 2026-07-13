"""Persistence for the relatedness graph (02-data-model §3, ADR-023).

The graph service depends on the :class:`GraphStore` *protocol*, not on asyncpg, so it unit-tests
against an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgGraphStore` is the
plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011).

Neighbours are computed **in the database** via pgvector: for each note, its top-K nearest other
notes over ``notes.embedding`` cosine, then filtered to the ``RELATED_MIN_SCORE`` floor. The
``vector`` codec on the pool (``db.py``) is not needed here — both operands of ``<=>`` are columns,
so the distance is evaluated entirely server-side against the HNSW index (migration 004).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class RelatedLink:
    """One neighbour of a note: the *related* note's id/path/title + cosine score (ADR-023)."""

    note_id: str
    vault_path: str
    title: str | None
    score: float


@dataclass(frozen=True)
class NoteNeighbors:
    """A source note and its directional top-K neighbours (highest score first)."""

    note_id: str
    vault_path: str
    related: list[RelatedLink] = field(default_factory=list)


class GraphStore(Protocol):
    """The persistence surface the relatedness-graph service relies on."""

    async def compute_neighbors(self, *, top_k: int, min_score: float) -> list[NoteNeighbors]:
        """Per note, its top-K nearest other notes above ``min_score`` (cosine). Only notes with
        at least one qualifying neighbour appear. Notes without an embedding are ignored."""
        ...

    async def replace_note_links(self, neighbors: list[NoteNeighbors]) -> int:
        """Replace the whole ``note_links`` table with these directional edges, in one
        transaction. Returns the number of edge rows written."""
        ...

    async def list_note_paths(self) -> list[str]:
        """Every indexed note's ``vault_path`` — so the render pass can also strip a stale block
        from a note that has *lost* all its neighbours."""
        ...


class PgGraphStore:
    """asyncpg-backed graph store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def compute_neighbors(self, *, top_k: int, min_score: float) -> list[NoteNeighbors]:
        # Per source note n, a LATERAL picks its top_k nearest other notes by cosine distance;
        # the outer WHERE then drops any below the score floor (so a note keeps *at most* top_k,
        # possibly fewer). score = 1 - cosine distance. The distance is written
        # ``m.embedding <=> n.embedding`` (indexed column on the left) so the planner can use the
        # HNSW index on ``notes.embedding`` (migration 004). ``m.id`` is a deterministic
        # tiebreaker on both the top_k cut and the render order, so exact-score ties can't reorder
        # the block between runs and spuriously churn the vault (ADR-023 §5 — zero-churn contract).
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    n.id          AS note_id,
                    n.vault_path  AS vault_path,
                    m.id          AS related_note_id,
                    m.vault_path  AS related_vault_path,
                    m.title       AS related_title,
                    1 - (m.embedding <=> n.embedding) AS score
                FROM notes n
                CROSS JOIN LATERAL (
                    SELECT id, vault_path, title, embedding
                    FROM notes m
                    WHERE m.id <> n.id AND m.embedding IS NOT NULL
                    ORDER BY m.embedding <=> n.embedding, m.id
                    LIMIT $1
                ) m
                WHERE n.embedding IS NOT NULL
                  AND 1 - (m.embedding <=> n.embedding) >= $2
                ORDER BY n.id, score DESC, m.id
                """,
                top_k,
                min_score,
            )

        neighbors: list[NoteNeighbors] = []
        current: NoteNeighbors | None = None
        for row in rows:
            note_id = str(row["note_id"])
            if current is None or current.note_id != note_id:
                current = NoteNeighbors(
                    note_id=note_id, vault_path=row["vault_path"], related=[]
                )
                neighbors.append(current)
            current.related.append(
                RelatedLink(
                    note_id=str(row["related_note_id"]),
                    vault_path=row["related_vault_path"],
                    title=row["related_title"],
                    score=float(row["score"]),
                )
            )
        return neighbors

    async def replace_note_links(self, neighbors: list[NoteNeighbors]) -> int:
        edges = [
            (n.note_id, link.note_id, link.score)
            for n in neighbors
            for link in n.related
        ]
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM note_links")
            if edges:
                await conn.executemany(
                    """
                    INSERT INTO note_links (note_id, related_note_id, score)
                    VALUES ($1, $2, $3)
                    """,
                    edges,
                )
        return len(edges)

    async def list_note_paths(self) -> list[str]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch("SELECT vault_path FROM notes")
        return [row["vault_path"] for row in rows]
