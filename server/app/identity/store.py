"""Identity-capsule persistence + source reads (M5 task 2, ADR-046 §5 / ADR-033 #1).

Two concerns, two protocols, so the distiller (and the ``build_context`` / chat readers) unit-test
against fakes with no live DB (08 testing policy):

  * :class:`CapsuleStore` — the derived blob in ``app_settings`` under one ``identity_capsule`` key
    (``{text, generated_at, source_refs}``): the write side the nightly job uses + the cheap
    :meth:`current` read that ``build_context`` L0 and the chat system prompt (and the MCP
    ``identity://me`` resource, task 4) serve. Rebuildable, no new table (rule 1).
  * :class:`CapsuleSourceStore` — the broadened source material the distiller blends: the
    highest-degree entity-profile **hubs**, **recent memories**, and **recent insights** (ADR-046
    §5). Every read excludes tombstones (``merged_into`` set).

Plain SQL over asyncpg, no ORM (rule 5, ADR-011).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..db import Database

# The single ``app_settings`` key holding the derived capsule blob (02-data-model §3).
CAPSULE_KEY = "identity_capsule"


@dataclass(frozen=True)
class CapsuleBlob:
    """The derived identity capsule (02-data-model §3): the distilled text + when it was generated +
    the source nodes it was distilled from (``{node_id, title, kind}`` refs, for provenance)."""

    text: str
    generated_at: datetime | None = None
    source_refs: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class HubProfile:
    """A high-degree entity-profile hub — the distiller's primary source material (its ``profile``
    is the already-distilled per-entity summary the profile-refresh job produced)."""

    node_id: str
    title: str | None
    type: str
    tier: str
    profile: str
    degree: int


@dataclass(frozen=True)
class RecentNode:
    """A recent memory or insight node fed to the distiller (title + short first-chunk excerpt)."""

    node_id: str
    title: str | None
    type: str
    plane: str | None
    excerpt: str | None


class IdentityCapsuleReader(Protocol):
    """The cheap capsule read ``build_context`` L0 + the chat system prompt depend on (a narrow
    subset of :class:`CapsuleStore` — they never write). ``None`` when no capsule exists yet."""

    async def current(self) -> CapsuleBlob | None: ...


class CapsuleStore(IdentityCapsuleReader, Protocol):
    """Read + write the derived capsule blob (the nightly job's persistence surface)."""

    async def save(self, blob: CapsuleBlob) -> None: ...


class CapsuleSourceStore(Protocol):
    """The broadened source reads the distiller blends (ADR-046 §5)."""

    async def top_profile_hubs(self, limit: int) -> list[HubProfile]: ...

    async def recent_memories(self, limit: int) -> list[RecentNode]: ...

    async def recent_insights(self, limit: int) -> list[RecentNode]: ...


class PgIdentityCapsuleStore:
    """asyncpg-backed capsule blob store over ``app_settings`` — plain SQL (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def current(self) -> CapsuleBlob | None:
        async with self._db.acquire() as conn:
            value = await conn.fetchval(
                "SELECT value FROM app_settings WHERE key = $1", CAPSULE_KEY
            )
        return _decode_blob(value)

    async def save(self, blob: CapsuleBlob) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = $2::jsonb, updated_at = now()
                """,
                CAPSULE_KEY,
                json.dumps(_encode_blob(blob)),
            )


class PgCapsuleSourceStore:
    """asyncpg-backed source reads for the distiller — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def top_profile_hubs(self, limit: int) -> list[HubProfile]:
        if limit <= 0:
            return []
        # The entity-profile hubs ranked by graph degree (ADR-046 §5): canonical-edge count on the
        # node (both directions), highest first — the people/things the graph is most about. Only
        # live nodes that already carry a derived profile; ties broken by freshest profile.
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.node_id, n.title, n.type, p.tier, p.profile,
                       (SELECT count(*) FROM edges e
                          WHERE (e.src_id = p.node_id OR e.dst_id = p.node_id)
                            AND e.origin = 'canonical') AS degree
                FROM node_profiles p
                JOIN nodes n ON n.id = p.node_id
                WHERE n.merged_into IS NULL
                ORDER BY degree DESC, p.refreshed_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            HubProfile(
                node_id=str(r["node_id"]),
                title=r["title"],
                type=r["type"],
                tier=r["tier"],
                profile=r["profile"],
                degree=int(r["degree"]),
            )
            for r in rows
        ]

    async def recent_memories(self, limit: int) -> list[RecentNode]:
        return await self._recent_of_type("memory", limit)

    async def recent_insights(self, limit: int) -> list[RecentNode]:
        return await self._recent_of_type("insight", limit)

    async def _recent_of_type(self, node_type: str, limit: int) -> list[RecentNode]:
        # The most recent live nodes of one kind, newest-first by event time (``occurred_start``),
        # falling back to ingest time when undated. The first chunk supplies a short excerpt so the
        # distiller sees more than a bare title without a second fetch.
        if limit <= 0:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT n.id, n.title, n.type, n.plane,
                       (SELECT c.content FROM chunks c
                          WHERE c.node_id = n.id ORDER BY c.chunk_index LIMIT 1) AS excerpt
                FROM nodes n
                WHERE n.type = $1 AND n.merged_into IS NULL
                ORDER BY COALESCE(n.occurred_start, n.node_created_at::date, n.indexed_at::date)
                             DESC,
                         n.indexed_at DESC
                LIMIT $2
                """,
                node_type,
                limit,
            )
        return [
            RecentNode(
                node_id=str(r["id"]),
                title=r["title"],
                type=r["type"],
                plane=r["plane"],
                excerpt=r["excerpt"],
            )
            for r in rows
        ]


def _encode_blob(blob: CapsuleBlob) -> dict[str, Any]:
    return {
        "text": blob.text,
        "generated_at": blob.generated_at.isoformat() if blob.generated_at else None,
        "source_refs": list(blob.source_refs),
    }


def _decode_blob(value: Any) -> CapsuleBlob | None:
    """Decode the ``identity_capsule`` jsonb into a :class:`CapsuleBlob` (``None`` on absent/junk/
    empty text — the readers then omit the capsule, never surface a broken one)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            obj: Any = json.loads(value)
        except ValueError:
            return None
    else:
        obj = value
    if not isinstance(obj, dict):
        return None
    text = obj.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    refs = obj.get("source_refs")
    source_refs = [r for r in refs if isinstance(r, dict)] if isinstance(refs, list) else []
    return CapsuleBlob(
        text=text,
        generated_at=_parse_dt(obj.get("generated_at")),
        source_refs=source_refs,
    )


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive stored value predates timezone-awareness — treat it as UTC (the app writes UTC).
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
