"""M9.8 T1 — durable, replayable entity merges (ADR-064 §1).

Revision ID: 021
Revises: 020
Create Date: 2026-07-19

Hand-authored plain SQL (ADR-011). Records each entity merge as a **durable decision keyed on
stable identity — the loser's surface forms (name + aliases) + node type — not its node id** — so
``reprocess-all`` can re-apply it after the raw rebuild mints fresh ids (the gap ADR-042 §4 could
only *warn* about: "standing merges the rebuild can't re-apply by id"). This is the same durability
posture as removed-capture tombstones (``captures.removed_at``): the raw is truth, and the merge is
a replayed decision on top of it. See ``app.entities.merge_store`` +
``second-brain-docs/adr/064-durable-merges-visual-dedup-gc.md`` §1.

``entity_merges``:
* ``survivor_forms`` / ``loser_forms`` — the two sides' **normalized** surface forms (folded +
  lower-cased + whitespace-collapsed, matching ``app.entities.store.normalize_alias``), captured at
  merge time *before* the alias union. Replay resolves each side to a live re-created hub of the
  recorded type whose title/alias matches one of these forms, then re-folds loser → survivor.
* ``survivor_type`` / ``loser_type`` — the entity types the two forms must match (an entity merge is
  entity-like on both sides; the type narrows the replay lookup and guards against a same-name hub
  of a different type being pulled in).
* ``loser_key`` — a deterministic idempotency key (``loser_type`` + the sorted ``loser_forms``); a
  **unique** index makes ``record`` an upsert, so re-merging the same loser (or applying the same
  merge twice) updates the one decision (last survivor wins) rather than accumulating duplicates.
* ``survivor_node_id`` / ``loser_node_id`` — the ids at merge time, for observability only
  (``text``, not an FK: after a reprocess these ids no longer exist, which is the whole reason the
  decision is keyed on surface form instead).

Additive + reversible; no existing table is touched.
"""

from __future__ import annotations

from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE entity_merges (
            id                uuid        PRIMARY KEY,
            survivor_type     text        NOT NULL,
            survivor_forms    text[]      NOT NULL,
            loser_type        text        NOT NULL,
            loser_forms       text[]      NOT NULL,
            loser_key         text        NOT NULL,
            survivor_node_id  text,
            loser_node_id     text,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One durable decision per loser identity (type + sorted forms): the upsert conflict target, so
    # a repeated/re-applied merge overwrites rather than duplicates (ADR-064 §1 idempotency).
    op.execute("CREATE UNIQUE INDEX entity_merges_loser_key ON entity_merges (loser_key)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_merges")
