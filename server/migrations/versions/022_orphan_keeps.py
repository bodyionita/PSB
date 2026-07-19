"""M9.8 T5.5 — durable orphan keep-list (ADR-064 §5).

Revision ID: 022
Revises: 021
Create Date: 2026-07-19

Hand-authored plain SQL (ADR-011). Records each intentionally-kept zero-degree entity **hub** (e.g.
Father/Mother) as a **durable decision keyed on stable identity — the hub's normalized surface forms
(name + aliases) + node type — not its node id** — so a keep **survives ``reprocess-all``** with
**no replay step**: the graph-health orphan check applies it as a **read-time filter** (a hub whose
surface forms intersect a kept entry of the same type is excluded from the orphan count + sample),
not a mutation the rebuild would undo. This is the same durability posture as ``entity_merges``
(migration 021) — governance replayed (here, as a filter) on top of the raw-rebuilt graph, never
baked into raw. See ``app.entities.keep_store`` +
``second-brain-docs/adr/064-durable-merges-visual-dedup-gc.md`` §5.

``orphan_keeps``:
* ``node_type`` / ``forms`` — the hub's entity type + its **normalized** surface forms (folded +
  lower-cased + whitespace-collapsed via ``app.entities.store.normalize_alias``, title first),
  captured at keep time. The filter matches a live orphan hub of the same ``node_type`` whose
  surface forms intersect these.
* ``keep_key`` — a deterministic idempotency key (``node_type`` + the sorted ``forms``); a
  **unique** index makes ``record`` an upsert, so re-keeping the same hub updates the one decision
  rather than accumulating duplicates. Also the stable handle ``DELETE /admin/orphan-keeps/{key}``
  un-keys on (a reprocess changes the node id, the key persists).
* ``node_id`` — the id at keep time, for observability only (``text``, not an FK: after a reprocess
  it no longer exists, which is the whole reason the decision is keyed on surface form instead).

Additive + reversible; no existing table is touched.
"""

from __future__ import annotations

from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE orphan_keeps (
            id          uuid        PRIMARY KEY,
            node_type   text        NOT NULL,
            forms       text[]      NOT NULL,
            keep_key    text        NOT NULL,
            node_id     text,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One durable decision per kept-hub identity (type + sorted forms): the upsert conflict target,
    # so re-keeping the same hub overwrites rather than duplicates (ADR-064 §5 idempotency), and the
    # stable handle the un-keep endpoint deletes on.
    op.execute("CREATE UNIQUE INDEX orphan_keeps_keep_key ON orphan_keeps (keep_key)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS orphan_keeps")
