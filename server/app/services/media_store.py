"""Media persistence — the DB rows + the on-disk raw files (02-data-model, ADR-057 §3).

Two collaborating pieces, mirroring the capture pipeline's split (a ``captures`` row + the audio
file under ``DATA_PATH``):

* :class:`MediaStore` (protocol) / :class:`PgMediaStore` — the ``media`` table rows: kind, source,
  the raw file's relative path, the derivation lifecycle (``pending`` → ``derived`` |
  ``unavailable``), derived text + model, retry accounting. Plain SQL over asyncpg (rule 5); the
  derivation service depends on the protocol so it unit-tests against an in-memory fake.
* :class:`MediaFiles` — the filesystem side: raw media live under
  ``<DATA_PATH>/<MEDIA_FOLDER>/<source>/…`` (ADR-057 §3 — never in the git store, never a DB blob;
  the ``/srv/data`` volume already R2-syncs, ADR-014). Paths stored on the row are **relative** to
  the media root, so a volume move never rewrites the DB.

Derived text is derived-tier (recomputable from the kept raw under a better model — P10), with the
single recorded exception of video summaries (ADR-057 §2: video is summary-only, ``file_path``
NULL, produced at import).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..db import Database

# --- Media kinds (ADR-057 §1/§3). ---
KIND_PHOTO = "photo"
KIND_VOICE = "voice"
KIND_VIDEO = "video"

# --- Derivation lifecycle statuses (ADR-057 §3). ---
PENDING = "pending"
DERIVED = "derived"
UNAVAILABLE = "unavailable"


@dataclass
class MediaRecord:
    id: str
    kind: str
    source: str
    status: str
    capture_id: str | None = None
    file_path: str | None = None
    thumb_path: str | None = None
    mime_type: str | None = None
    derived_text: str | None = None
    model_used: str | None = None
    attempts: int = 0
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MediaStore(Protocol):
    """The ``media``-table surface the derivation stage + serving endpoint rely on."""

    async def create(
        self,
        *,
        kind: str,
        source: str,
        capture_id: str | None = None,
        file_path: str | None = None,
        thumb_path: str | None = None,
        mime_type: str | None = None,
        status: str = PENDING,
        derived_text: str | None = None,
        model_used: str | None = None,
    ) -> MediaRecord: ...

    async def get(self, media_id: str) -> MediaRecord | None: ...

    async def get_many(self, media_ids: list[str]) -> list[MediaRecord]:
        """The given ids that exist, in ``created_at`` order (an unknown id is simply absent)."""
        ...

    async def list_by_status(self, status: str, *, limit: int) -> list[MediaRecord]:
        """Rows in ``status`` (e.g. ``unavailable`` for re-derive), oldest-first, bounded."""
        ...

    async def mark_derived(
        self, media_id: str, *, derived_text: str, model_used: str | None, attempts: int
    ) -> None:
        """Derivation succeeded: ``status=derived`` + text/model, clear the error."""
        ...

    async def mark_unavailable(self, media_id: str, *, error: str, attempts: int) -> None:
        """Bounded retries exhausted: ``status=unavailable`` (explicit placeholder downstream) —
        reversible, targeted re-derive can reset it because raw is kept (ADR-057 §3)."""
        ...

    async def mark_retry(self, media_id: str, *, error: str, attempts: int) -> None:
        """A failed attempt with retries left: stay ``pending``, bump attempts, record why."""
        ...

    async def reset_to_pending(self, media_ids: list[str]) -> int:
        """Targeted re-derive: put items back to ``pending`` with attempts cleared (a fresh chance
        now that the cause may be fixed). Returns the number of rows reset."""
        ...


_COLUMNS = (
    "id, kind, source, capture_id, file_path, thumb_path, mime_type, status, "
    "derived_text, model_used, attempts, error, created_at, updated_at"
)


def _record(row) -> MediaRecord:
    return MediaRecord(
        id=str(row["id"]),
        kind=row["kind"],
        source=row["source"],
        status=row["status"],
        capture_id=str(row["capture_id"]) if row["capture_id"] is not None else None,
        file_path=row["file_path"],
        thumb_path=row["thumb_path"],
        mime_type=row["mime_type"],
        derived_text=row["derived_text"],
        model_used=row["model_used"],
        attempts=row["attempts"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PgMediaStore:
    """asyncpg-backed ``media`` store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        kind: str,
        source: str,
        capture_id: str | None = None,
        file_path: str | None = None,
        thumb_path: str | None = None,
        mime_type: str | None = None,
        status: str = PENDING,
        derived_text: str | None = None,
        model_used: str | None = None,
    ) -> MediaRecord:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO media
                    (kind, source, capture_id, file_path, thumb_path, mime_type,
                     status, derived_text, model_used)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING {_COLUMNS}
                """,
                kind,
                source,
                capture_id,
                file_path,
                thumb_path,
                mime_type,
                status,
                derived_text,
                model_used,
            )
        return _record(row)

    async def get(self, media_id: str) -> MediaRecord | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM media WHERE id = $1", media_id)
        return _record(row) if row is not None else None

    async def get_many(self, media_ids: list[str]) -> list[MediaRecord]:
        if not media_ids:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM media WHERE id = ANY($1::uuid[]) ORDER BY created_at ASC",
                media_ids,
            )
        return [_record(r) for r in rows]

    async def list_by_status(self, status: str, *, limit: int) -> list[MediaRecord]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS} FROM media
                 WHERE status = $1
                 ORDER BY created_at ASC
                 LIMIT $2
                """,
                status,
                limit,
            )
        return [_record(r) for r in rows]

    async def _set(self, media_id: str, assignment: str, *values) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                f"UPDATE media SET {assignment}, updated_at = now() WHERE id = $1",
                media_id,
                *values,
            )

    async def mark_derived(
        self, media_id: str, *, derived_text: str, model_used: str | None, attempts: int
    ) -> None:
        await self._set(
            media_id,
            "status = $2, derived_text = $3, model_used = $4, attempts = $5, error = NULL",
            DERIVED,
            derived_text,
            model_used,
            attempts,
        )

    async def mark_unavailable(self, media_id: str, *, error: str, attempts: int) -> None:
        await self._set(
            media_id, "status = $2, attempts = $3, error = $4", UNAVAILABLE, attempts, error
        )

    async def mark_retry(self, media_id: str, *, error: str, attempts: int) -> None:
        await self._set(
            media_id, "status = $2, attempts = $3, error = $4", PENDING, attempts, error
        )

    async def reset_to_pending(self, media_ids: list[str]) -> int:
        if not media_ids:
            return 0
        async with self._db.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE media
                   SET status = $1, attempts = 0, error = NULL, updated_at = now()
                 WHERE id = ANY($2::uuid[])
                """,
                PENDING,
                media_ids,
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0


class MediaFiles:
    """Filesystem side of media storage: raw files under ``<DATA_PATH>/<MEDIA_FOLDER>/…`` (ADR-057
    §3). Blocking IO — callers wrap in ``asyncio.to_thread`` (rule 8). Paths handed to/from the DB
    are **relative** to the media root (``<source>/<name>``), so a volume move never rewrites the
    rows."""

    def __init__(self, settings: Settings) -> None:
        self._root = Path(settings.data_path) / settings.media_folder

    def relative_path(self, source: str, name: str) -> str:
        """The stored relative path for a media file: ``<source>/<name>`` (ADR-057 §3 layout)."""
        return f"{source}/{name}"

    def absolute(self, relative_path: str) -> Path:
        return self._root / relative_path

    def write(self, relative_path: str, data: bytes) -> None:
        path = self.absolute(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, relative_path: str) -> bytes:
        return self.absolute(relative_path).read_bytes()

    async def read_async(self, relative_path: str) -> bytes:
        return await asyncio.to_thread(self.read, relative_path)

    async def exists_async(self, relative_path: str) -> bool:
        """Whether the file is present — the blocking ``stat`` runs off the event loop (rule 8)."""
        return await asyncio.to_thread(self.absolute(relative_path).is_file)

    async def write_async(self, relative_path: str, data: bytes) -> None:
        await asyncio.to_thread(self.write, relative_path, data)


def build_media_files(settings: Settings) -> MediaFiles:
    return MediaFiles(settings)
