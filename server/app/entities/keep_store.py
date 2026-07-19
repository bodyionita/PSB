"""Durable orphan-keep decisions — the read-time whitelist behind ADR-064 §5 (02-data-model §3).

An intentionally-kept zero-degree entity **hub** (Father/Mother, …) should stop nagging the nightly
graph-health orphan check. A keep keyed on the hub's **node id** would be silently lost on
``reprocess-all`` (fresh ids from raw) — the same trap ADR-042 §4 hit for merges. So this store
records each keep as a durable decision keyed on **stable identity — the hub's normalized surface
forms (name + aliases) + node type — not its id**. Unlike a merge it needs **no replay step**: the
graph-health orphan check consumes it as a **read-time filter** (a live orphan hub of the same type
whose surface forms intersect a kept entry is excluded from the count + sample), so a kept hub
survives a reprocess with nothing to re-apply.

Plain SQL over asyncpg (rule 5, ADR-011); callers depend on the :class:`KeepStore` protocol so they
unit-test against a fake. :class:`PgKeepStore` is the implementation; ``record`` is an upsert on the
keep identity key (idempotent — re-keeping the same hub updates the one row). ``surface_forms`` is
shared with the merge store so keep + merge normalize identity identically (ADR-064 §1/§5).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..db import Database
from .merge_store import surface_forms

__all__ = ["surface_forms", "keep_key", "KeepDecision", "KeepStore", "PgKeepStore"]


def keep_key(node_type: str, forms: Iterable[str]) -> str:
    """The idempotency key for a durable keep: the hub's type + its sorted normalized forms.
    Deterministic across re-keeps of the same hub, so the store upserts one row (ADR-064 §5) and the
    un-keep endpoint has a stable handle. The canonical identity joins on NUL / SOH (which can't
    occur in a normalized surface form, so the join is unambiguous — as in
    ``merge_store.loser_key``) then **base64url-encodes** it: unlike a merge decision this key
    travels in a URL path (``DELETE /admin/orphan-keeps/{key}``), and a raw NUL (present in *every*
    key as the type/forms separator) or a ``/`` inside a surface form would be rejected/normalized
    by the Cloudflare + Caddy ingress (07-infra). base64url (``A-Za-z0-9-_``, padding stripped) is
    path-safe and stays a stable, reversible, opaque handle (the node_type + forms columns carry the
    readable identity)."""
    canonical = node_type + "\x00" + "\x01".join(sorted(forms))
    return base64.urlsafe_b64encode(canonical.encode("utf-8")).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class KeepDecision:
    """One durable keep decision — the hub identity the orphan filter matches (ADR-064 §5).

    ``forms`` are normalized (via :func:`surface_forms`) and title-first, captured at keep time.
    ``node_id`` is the keep-time id, kept for observability only (it no longer resolves after a
    reprocess). ``created_at`` is set by the store on read (the "kept at" the web strip shows)."""

    node_type: str
    forms: list[str]
    node_id: str | None = None
    created_at: datetime | None = None

    @property
    def key(self) -> str:
        return keep_key(self.node_type, self.forms)

    @property
    def label(self) -> str:
        """A short human label for the "Kept (N)" strip — the title form (``forms`` is title-first),
        or the type when a hub somehow carried no surface form."""
        return self.forms[0] if self.forms else self.node_type


class KeepStore(Protocol):
    """The durable-keep read/write surface the keep service + graph-health filter share."""

    async def record(self, decision: KeepDecision) -> None:
        """Persist (upsert on the keep identity key) one keep decision (ADR-064 §5)."""
        ...

    async def all_keeps(self) -> list[KeepDecision]:
        """Every recorded keep, newest first — the "Kept (N)" strip + the orphan-filter source."""
        ...

    async def remove(self, key: str) -> bool:
        """Un-keep by ``keep_key``; returns whether a row was removed (``False`` → 404)."""
        ...


class PgKeepStore:
    """asyncpg-backed durable-keep store — plain SQL, no ORM (ADR-011)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, decision: KeepDecision) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orphan_keeps (id, node_type, forms, keep_key, node_id)
                VALUES ($1, $2, $3::text[], $4, $5)
                ON CONFLICT (keep_key) DO UPDATE SET
                    node_type  = EXCLUDED.node_type,
                    forms      = EXCLUDED.forms,
                    node_id    = EXCLUDED.node_id,
                    created_at = now()
                """,
                str(uuid.uuid4()),
                decision.node_type,
                decision.forms,
                decision.key,
                decision.node_id,
            )

    async def all_keeps(self) -> list[KeepDecision]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT node_type, forms, node_id, created_at
                FROM orphan_keeps
                ORDER BY created_at DESC, keep_key ASC
                """
            )
        return [
            KeepDecision(
                node_type=r["node_type"],
                forms=list(r["forms"] or []),
                node_id=r["node_id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def remove(self, key: str) -> bool:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM orphan_keeps WHERE keep_key = $1 RETURNING id", key
            )
        return row is not None
