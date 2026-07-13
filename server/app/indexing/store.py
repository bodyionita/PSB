"""Persistence for the derived search index (02-data-model §3).

The indexer depends on the :class:`IndexStore` *protocol*, not on asyncpg, so it unit-tests
against an in-memory fake (no live DB in CI — 08 testing policy). :class:`PgIndexStore` is the
plain-SQL asyncpg implementation (CLAUDE.md rule 5, ADR-011).

The core write is :meth:`upsert_note`: one **per-note transaction** that replaces a note's row
and *all* its chunks atomically (02 §3, ADR-022) — a note is never left half-indexed. Embeddings
pass as plain Python float lists: the ``vector`` codec registered on the asyncpg pool (see
``db.py``) encodes them, and the column type enforces the 768 dimension (migration 004).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class NoteChunk:
    """One retrieval chunk destined for the ``chunks`` table."""

    index: int
    content: str
    embedding: list[float]


@dataclass(frozen=True)
class NoteUpsert:
    """A fully-prepared ``notes`` row plus its chunks — the unit of a per-note transaction.

    ``embedding`` is the note-level mean-pool vector (``None`` when the note produced no chunks);
    ``chunks`` may be empty for an empty note (the row is still tracked so its hash skips it next
    time).
    """

    vault_path: str
    content_hash: str
    title: str | None = None
    plane: str | None = None
    planes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    source_ref: str | None = None
    note_created_at: datetime | None = None
    embedding: list[float] | None = None
    chunks: list[NoteChunk] = field(default_factory=list)


class IndexStore(Protocol):
    """The index persistence surface the indexer relies on."""

    async def get_content_hash(self, vault_path: str) -> str | None:
        """Current ``content_hash`` for a note, or ``None`` if it isn't indexed yet."""
        ...

    async def upsert_note(self, note: NoteUpsert) -> None:
        """Replace a note's row + all its chunks in one transaction (delete old chunks, upsert
        the note, insert the new chunks). Idempotent on ``vault_path``."""
        ...

    async def list_indexed_paths(self) -> set[str]:
        """Every ``vault_path`` currently in the index (for deletion reconciliation on rescan)."""
        ...

    async def delete_notes(self, vault_paths: list[str]) -> int:
        """Delete notes by path (``chunks``/``note_links`` cascade). Returns the row count."""
        ...


class PgIndexStore:
    """asyncpg-backed index store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_content_hash(self, vault_path: str) -> str | None:
        async with self._db.acquire() as conn:
            return await conn.fetchval(
                "SELECT content_hash FROM notes WHERE vault_path = $1", vault_path
            )

    async def upsert_note(self, note: NoteUpsert) -> None:
        async with self._db.transaction() as conn:
            note_id = await conn.fetchval(
                """
                INSERT INTO notes (
                    vault_path, title, plane, planes, tags, source, source_ref,
                    content_hash, note_created_at, embedding, indexed_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
                ON CONFLICT (vault_path) DO UPDATE SET
                    title = EXCLUDED.title,
                    plane = EXCLUDED.plane,
                    planes = EXCLUDED.planes,
                    tags = EXCLUDED.tags,
                    source = EXCLUDED.source,
                    source_ref = EXCLUDED.source_ref,
                    content_hash = EXCLUDED.content_hash,
                    note_created_at = EXCLUDED.note_created_at,
                    embedding = EXCLUDED.embedding,
                    indexed_at = now()
                RETURNING id
                """,
                note.vault_path,
                note.title,
                note.plane,
                note.planes,
                note.tags,
                note.source,
                note.source_ref,
                note.content_hash,
                note.note_created_at,
                note.embedding,
            )
            # Replace the chunk set wholesale (a note is re-chunked on every reindex).
            await conn.execute("DELETE FROM chunks WHERE note_id = $1", note_id)
            if note.chunks:
                await conn.executemany(
                    """
                    INSERT INTO chunks (note_id, chunk_index, content, embedding)
                    VALUES ($1, $2, $3, $4)
                    """,
                    [(note_id, c.index, c.content, c.embedding) for c in note.chunks],
                )

    async def list_indexed_paths(self) -> set[str]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch("SELECT vault_path FROM notes")
        return {row["vault_path"] for row in rows}

    async def delete_notes(self, vault_paths: list[str]) -> int:
        if not vault_paths:
            return 0
        async with self._db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM notes WHERE vault_path = ANY($1::text[])", vault_paths
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0
