"""Entity-service DB reads (02-data-model §3, ADR-030 §4/§5/§6) — the shared query surface for the
merge, backfill, and profile-refresh services (M3 task 6).

Plain SQL over asyncpg (rule 5, ADR-011); the services depend on the :class:`EntityStore`
*protocol* so they unit-test against fakes (no live DB in CI — 08 testing policy).
:class:`PgEntityStore` is the implementation. Every read excludes tombstones (``merged_into`` set)
so a merged node is never returned as a live entity, candidate, or neighbor (ADR-030 §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class EntityNode:
    """A single node's identity + entity fields, for merge validation + the alias union."""

    id: str
    type: str
    title: str | None
    store_path: str
    aliases: list[str]
    merged_into: str | None


@dataclass(frozen=True)
class InboundEdge:
    """A canonical edge pointing at a node (the reverse index the merge rewrites — ADR-030 §5)."""

    src_id: str
    src_store_path: str
    rel: str


@dataclass(frozen=True)
class EntityRef:
    """An entity-like node the backfill/profile jobs iterate (id + type + aliases + path)."""

    id: str
    type: str
    title: str | None
    aliases: list[str]
    store_path: str


@dataclass(frozen=True)
class Neighbor:
    """One 1-hop canonical neighbor of an entity — the raw material of a derived profile.

    ``rel`` is the connecting relation, ``dir`` is ``in`` (neighbor → entity) or ``out`` (entity →
    neighbor); ``since``/``until`` are the edge validity window, ``occurred_start`` the neighbor's
    event time — together they carry the ``(as of …)`` stamps (ADR-032/034)."""

    node_id: str
    type: str
    title: str | None
    plane: str | None
    rel: str
    dir: str
    since: date | None
    until: date | None
    occurred_start: date | None


@dataclass(frozen=True)
class AliasMatchNode:
    """A recent memory node whose text mentions an entity alias but has no edge to it (backfill)."""

    node_id: str
    store_path: str
    excerpt: str


class EntityStore(Protocol):
    """The entity-read surface the merge/backfill/profile services rely on."""

    async def get_node(self, node_id: str) -> EntityNode | None: ...

    async def find_entity_by_surface_forms(
        self, forms: list[str], *, node_type: str
    ) -> str | None: ...

    async def inbound_canonical_edges(self, node_id: str) -> list[InboundEdge]: ...

    async def list_entities(self, *, types: list[str]) -> list[EntityRef]: ...

    async def entities_touched_since(
        self, *, types: list[str], since: datetime
    ) -> list[EntityRef]: ...

    async def neighborhood(self, node_id: str) -> list[Neighbor]: ...

    async def memory_nodes_matching_alias(
        self, alias: str, *, entity_id: str, window_start: datetime, limit: int
    ) -> list[AliasMatchNode]: ...


class PgEntityStore:
    """asyncpg-backed entity reads — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_node(self, node_id: str) -> EntityNode | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, type, title, store_path, aliases, merged_into
                FROM nodes WHERE id = $1
                """,
                node_id,
            )
        if row is None:
            return None
        return EntityNode(
            id=str(row["id"]),
            type=row["type"],
            title=row["title"],
            store_path=row["store_path"],
            aliases=list(row["aliases"] or []),
            merged_into=str(row["merged_into"]) if row["merged_into"] else None,
        )

    async def find_entity_by_surface_forms(self, forms: list[str], *, node_type: str) -> str | None:
        # Resolve a durable merge decision's side to a live re-created hub (ADR-064 §1): a
        # non-tombstone entity of `node_type` whose normalized title/alias matches one of `forms`
        # (already normalized by `surface_forms`). The stored title/alias is normalized the same way
        # inline (lower + collapse whitespace; diacritics already folded on write, ADR-041). The
        # **title form ranks first** (`forms[0]` is the recorded title), so when survivor and loser
        # share a short alias (both carry "diana") each still resolves to its own hub by title, not
        # crossing. A tie (identical titles) returns one deterministically; the replay's
        # survivor==loser guard then skips it (can't distinguish → don't merge). Cold, not hot path.
        if not forms:
            return None
        primary = forms[0]
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                r"""
                SELECT id
                FROM nodes
                WHERE type = $2 AND merged_into IS NULL
                  AND (
                        btrim(regexp_replace(lower(title), '\s+', ' ', 'g')) = ANY($1::text[])
                     OR EXISTS (SELECT 1 FROM unnest(aliases) a
                                WHERE btrim(regexp_replace(lower(a), '\s+', ' ', 'g'))
                                      = ANY($1::text[]))
                  )
                ORDER BY
                  (btrim(regexp_replace(lower(title), '\s+', ' ', 'g')) = $3) DESC,
                  (btrim(regexp_replace(lower(title), '\s+', ' ', 'g')) = ANY($1::text[])) DESC,
                  id
                LIMIT 1
                """,
                forms,
                node_type,
                primary,
            )
        return str(row["id"]) if row is not None else None

    async def inbound_canonical_edges(self, node_id: str) -> list[InboundEdge]:
        # The reverse index (edges_dst_idx): canonical edges whose target is this node, joined to
        # the source node's store path (merge rewrites those files). Derived edges are DB-only and
        # recomputed nightly, so they are ignored here. Live sources only (tombstones excluded).
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.src_id, n.store_path AS src_store_path, e.rel
                FROM edges e JOIN nodes n ON n.id = e.src_id
                WHERE e.dst_id = $1 AND e.origin = 'canonical' AND n.merged_into IS NULL
                ORDER BY n.store_path, e.rel
                """,
                node_id,
            )
        return [
            InboundEdge(src_id=str(r["src_id"]), src_store_path=r["src_store_path"], rel=r["rel"])
            for r in rows
        ]

    async def list_entities(self, *, types: list[str]) -> list[EntityRef]:
        if not types:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, type, title, aliases, store_path
                FROM nodes
                WHERE type = ANY($1::text[]) AND merged_into IS NULL
                ORDER BY id
                """,
                types,
            )
        return [_to_ref(r) for r in rows]

    async def entities_touched_since(self, *, types: list[str], since: datetime) -> list[EntityRef]:
        if not types:
            return []
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, type, title, aliases, store_path
                FROM nodes
                WHERE type = ANY($1::text[]) AND merged_into IS NULL AND indexed_at >= $2
                ORDER BY indexed_at DESC
                """,
                types,
                since,
            )
        return [_to_ref(r) for r in rows]

    async def neighborhood(self, node_id: str) -> list[Neighbor]:
        # 1-hop canonical neighbors, both directions (memory → entity is the common case; entity →
        # place `at` is the other). Tombstoned endpoints excluded. Derived `similar` edges are not
        # part of an entity's factual neighborhood, so origin='canonical' only.
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.rel, 'in' AS dir, n.id AS node_id, n.type, n.title, n.plane,
                       e.since, e.until, n.occurred_start
                FROM edges e JOIN nodes n ON n.id = e.src_id
                WHERE e.dst_id = $1 AND e.origin = 'canonical' AND n.merged_into IS NULL
                UNION ALL
                SELECT e.rel, 'out' AS dir, n.id AS node_id, n.type, n.title, n.plane,
                       e.since, e.until, n.occurred_start
                FROM edges e JOIN nodes n ON n.id = e.dst_id
                WHERE e.src_id = $1 AND e.origin = 'canonical' AND n.merged_into IS NULL
                ORDER BY dir, rel, node_id
                """,
                node_id,
            )
        return [
            Neighbor(
                node_id=str(r["node_id"]),
                type=r["type"],
                title=r["title"],
                plane=r["plane"],
                rel=r["rel"],
                dir=r["dir"],
                since=r["since"],
                until=r["until"],
                occurred_start=r["occurred_start"],
            )
            for r in rows
        ]

    async def memory_nodes_matching_alias(
        self, alias: str, *, entity_id: str, window_start: datetime, limit: int
    ) -> list[AliasMatchNode]:
        # Recent memory nodes whose chunk text mentions the alias (case-insensitive) but that carry
        # no edge to the entity yet — the backfill candidates (ADR-030 §6). A stricter word-boundary
        # regex would beat ILIKE '%alias%', but the substring match + the caller's length/entropy
        # guard keeps M3 simple; a best chunk supplies the excerpt.
        pattern = f"%{_escape_like(alias)}%"
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (n.id) n.id AS node_id, n.store_path, c.content AS excerpt
                FROM nodes n JOIN chunks c ON c.node_id = n.id
                WHERE n.type = 'memory' AND n.merged_into IS NULL
                  AND n.indexed_at >= $2
                  AND c.content ILIKE $1
                  AND NOT EXISTS (
                      SELECT 1 FROM edges e WHERE e.src_id = n.id AND e.dst_id = $3
                  )
                ORDER BY n.id
                LIMIT $4
                """,
                pattern,
                window_start,
                entity_id,
                limit,
            )
        return [
            AliasMatchNode(
                node_id=str(r["node_id"]),
                store_path=r["store_path"],
                excerpt=r["excerpt"],
            )
            for r in rows
        ]


def _to_ref(row: object) -> EntityRef:
    return EntityRef(
        id=str(row["id"]),
        type=row["type"],
        title=row["title"],
        aliases=list(row["aliases"] or []),
        store_path=row["store_path"],
    )


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards in a literal alias so ``%``/``_`` in a name match literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
