"""The `node_media` link store (ADR-060 §1–§4) — the node ↔ media attachment, derived-tier.

`media` rows hang off *captures* (`media.capture_id`, migration 017); this store owns the
first-class **node → media** link (migration 018) that makes a node's media visible on
`GET /nodes/{id}` + as `media_kinds` glyphs on search/chat cards. It is a **media attachment**, not
a graph edge (ADR-060 §1): it never touches `edges`, traverse, the Map, or MCP.

The link is **derived-tier** (ADR-060 §3), keyed on the raw-truth `media_id`: whenever a capture's
content nodes are (re)written — organize / retry / reorganize / `rederive_capture` /
`reprocess-all` — the pipeline recomputes it via :meth:`rebuild_for_media` (delete this media's
links, re-insert the current content nodes'). A merge repoints it loser→survivor in the shared
merge-core (:meth:`repoint`, ADR-060 §4). The node-detail read side (``GET /nodes/{id}.media[]``)
joins ``node_media`` in ``PgSearchStore.get_node`` (returning :class:`NodeMediaItem`s), so the
strip rides the same fetch as the node's edges.

Plain SQL over asyncpg (rule 5); the collaborators depend on the :class:`NodeMediaStore` protocol so
they unit-test against an in-memory fake (08 testing policy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..db import Database


@dataclass(frozen=True)
class NodeMediaItem:
    """One media item a node carries (ADR-060 §1) — the `GET /nodes/{id}.media[]` element. ``kind``
    (photo/voice/video) + ``status`` (pending/derived/unavailable) drive the surfacing strip;
    ``capture_id`` rides for the "see raw capture" traceability hop (ADR-060 §7)."""

    id: str
    kind: str
    status: str
    capture_id: str | None


class NodeMediaStore(Protocol):
    """The `node_media` surface the capture pipeline, merge-core, and node-detail read rely on."""

    async def rebuild_for_media(self, *, media_ids: list[str], node_ids: list[str]) -> None:
        """Derived-tier rebuild (ADR-060 §3): make ``media_ids`` link to exactly ``node_ids``. Keyed
        on the raw-truth media id — deletes every existing link for these media, then inserts the
        cartesian product against the current content nodes (``ON CONFLICT DO NOTHING``). Idempotent
        (rule 6): re-running with the same inputs is a no-op. Empty ``media_ids`` is a no-op (a
        text/chat capture has no media)."""
        ...

    async def repoint(self, *, loser_id: str, survivor_id: str) -> int:
        """Merge repoint (ADR-060 §4): move loser L's links onto survivor S. Insert S rows from L's
        (``ON CONFLICT DO NOTHING`` against the unique pair), then delete L's — a tombstoned loser
        is kept (no FK cascade fires), so the explicit repoint is what stops photos stranding on the
        tombstone. Returns the number of links repointed (pre-dedup)."""
        ...


class PgNodeMediaStore:
    """asyncpg-backed `node_media` store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def rebuild_for_media(self, *, media_ids: list[str], node_ids: list[str]) -> None:
        if not media_ids:
            return
        async with self._db.transaction() as conn:
            # Wipe this media's existing links (a prior organize's content nodes) then re-insert the
            # current set — the point of keying the rebuild on the stable media_id (ADR-060 §3).
            await conn.execute("DELETE FROM node_media WHERE media_id = ANY($1::uuid[])", media_ids)
            if not node_ids:
                return
            rows = [(node_id, media_id) for media_id in media_ids for node_id in node_ids]
            await conn.executemany(
                """
                INSERT INTO node_media (node_id, media_id)
                VALUES ($1, $2)
                ON CONFLICT (node_id, media_id) DO NOTHING
                """,
                rows,
            )

    async def repoint(self, *, loser_id: str, survivor_id: str) -> int:
        async with self._db.transaction() as conn:
            moved = await conn.fetch(
                """
                INSERT INTO node_media (node_id, media_id)
                SELECT $2, media_id FROM node_media WHERE node_id = $1
                ON CONFLICT (node_id, media_id) DO NOTHING
                RETURNING media_id
                """,
                loser_id,
                survivor_id,
            )
            deleted = await conn.execute("DELETE FROM node_media WHERE node_id = $1", loser_id)
        try:
            return int(deleted.split()[-1])
        except (ValueError, IndexError):
            return len(moved)


def build_node_media_store(db: Database) -> PgNodeMediaStore:
    return PgNodeMediaStore(db)
