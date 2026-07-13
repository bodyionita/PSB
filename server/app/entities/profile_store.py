"""Persistence for the derived entity profiles (``node_profiles``, migration 006 / ADR-030 §4).

The profile-refresh job depends on the :class:`ProfileStore` *protocol*, not on asyncpg, so it
unit-tests against a fake (no live DB in CI — 08 testing policy). :class:`PgProfileStore` is the
plain-SQL implementation (rule 5, ADR-011). Read-back for ``GET /nodes/{id}`` is served by the
search store's ``get_node`` (a LEFT JOIN, one query) — this store owns only the write side + the
neighborhood-hash read the job uses to skip unchanged entities.
"""

from __future__ import annotations

import json
from typing import Protocol

from ..db import Database


class ProfileStore(Protocol):
    """The profile-write surface the profile-refresh job relies on."""

    async def current_hash(self, node_id: str) -> str | None:
        """The neighborhood hash the stored profile was built from, or ``None`` if never built —
        the job skips regeneration when it equals the current neighborhood's hash."""
        ...

    async def upsert_profile(
        self,
        node_id: str,
        *,
        tier: str,
        profile: str,
        observations: list[dict],
        neighborhood_hash: str,
        embedding: list[float] | None,
    ) -> None:
        """Replace an entity's derived profile row (keyed on node_id). Idempotent."""
        ...


class PgProfileStore:
    """asyncpg-backed profile store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def current_hash(self, node_id: str) -> str | None:
        async with self._db.acquire() as conn:
            return await conn.fetchval(
                "SELECT neighborhood_hash FROM node_profiles WHERE node_id = $1", node_id
            )

    async def upsert_profile(
        self,
        node_id: str,
        *,
        tier: str,
        profile: str,
        observations: list[dict],
        neighborhood_hash: str,
        embedding: list[float] | None,
    ) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO node_profiles
                    (node_id, tier, profile, observations, neighborhood_hash, embedding,
                     refreshed_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, now())
                ON CONFLICT (node_id) DO UPDATE SET
                    tier = EXCLUDED.tier,
                    profile = EXCLUDED.profile,
                    observations = EXCLUDED.observations,
                    neighborhood_hash = EXCLUDED.neighborhood_hash,
                    embedding = EXCLUDED.embedding,
                    refreshed_at = now()
                """,
                node_id,
                tier,
                profile,
                json.dumps(observations),
                neighborhood_hash,
                embedding,
            )
