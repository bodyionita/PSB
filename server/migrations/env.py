"""Alembic environment (ADR-011).

Async, driven by the asyncpg driver so dev/prod parity holds. SQLAlchemy is imported here
and nowhere else in the codebase — it is a migration-only dependency and must not leak into
the runtime (CLAUDE.md rule 5). There are no ORM models and no ``target_metadata``; every
revision body is explicit SQL, so ``--autogenerate`` is intentionally unusable.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

# Make the app package importable so the DB URL comes from the single settings module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402

config = context.config


def _url_and_connect_args() -> tuple[str, dict]:
    """SQLAlchemy async URL + connect_args from the single settings DSN.

    The app runtime uses raw asyncpg, which parses ``sslmode`` from the DSN itself. But
    SQLAlchemy's asyncpg dialect forwards unknown query params straight to
    ``asyncpg.connect()``, which rejects ``sslmode`` (it wants ``ssl=``). So strip
    ``sslmode`` from the URL and pass it as asyncpg's ``ssl`` connect arg (asyncpg accepts
    the libpq sslmode strings — e.g. ``require`` — for that parameter).
    """
    parts = urlsplit(get_settings().database_url)
    scheme = "postgresql+asyncpg" if parts.scheme == "postgresql" else parts.scheme
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    connect_args: dict = {}
    sslmode = query.pop("sslmode", None)
    if sslmode:
        connect_args["ssl"] = sslmode
    url = urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url, connect_args


def run_migrations_offline() -> None:
    url, _ = _url_and_connect_args()
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url, connect_args = _url_and_connect_args()
    connectable = create_async_engine(
        url, poolclass=pool.NullPool, connect_args=connect_args
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
