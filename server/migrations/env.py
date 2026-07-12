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

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make the app package importable so the DB URL comes from the single settings module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402

config = context.config


def _database_url() -> str:
    url = get_settings().database_url
    # asyncpg dialect for SQLAlchemy's async engine; app runtime uses the plain DSN.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
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
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
