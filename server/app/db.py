"""Database access — the one module that owns the asyncpg pool (CLAUDE.md rule 5).

Plain SQL over asyncpg, no ORM. Migrations are Alembic (ADR-011); this module never
creates or alters schema.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from .config import Settings


def _encode_vector(value: list[float]) -> str:
    """Encode a Python float list as a pgvector text literal (``[0.1,0.2,…]``)."""
    return "[" + ",".join(repr(float(x)) for x in value) + "]"


def _decode_vector(text: str) -> list[float]:
    """Decode a pgvector text literal back into a float list (``[]`` for an empty vector)."""
    inner = text.strip()[1:-1].strip()
    return [float(x) for x in inner.split(",")] if inner else []


async def _register_vector_codec(conn: asyncpg.Connection) -> None:
    """Teach asyncpg the pgvector ``vector`` type (it has no built-in codec).

    Registered on every pooled connection so embeddings pass as plain Python lists — encode on
    INSERT, decode on SELECT — with no ``::vector`` casts or string juggling in query code. The
    ``vector`` type exists once migration 001's ``CREATE EXTENSION vector`` has run.
    """
    await conn.set_type_codec(
        "vector",
        encoder=_encode_vector,
        decoder=_decode_vector,
        format="text",
    )


class Database:
    """Owns a single asyncpg connection pool for the process lifetime."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self._settings.database_url,
            min_size=self._settings.db_pool_min_size,
            max_size=self._settings.db_pool_max_size,
            init=_register_vector_codec,
        )

    async def disconnect(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized; call connect() first.")
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    async def healthcheck(self) -> bool:
        """True if a trivial query succeeds. Never raises — health must not crash."""
        try:
            async with self.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except Exception:
            return False
