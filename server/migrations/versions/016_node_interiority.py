"""M8.2 task 2 — node `interiority` column (inner-voice dimension).

Revision ID: 016
Revises: 015
Create Date: 2026-07-17

Hand-authored plain SQL (ADR-011). Adds a nullable ``interiority`` column to ``nodes`` — the
``internal | external | mixed`` content dimension the organizer stamps on every content node
([ADR-055](adr/055-interiority-inner-voice-first-class.md) §1: *internal* = the user's inner
voice, *external* = a record of the world, *mixed* = both after extraction). It is orthogonal to
``type`` (a dimension of content, not a kind), and the sole schema change of the interiority half
of M8.2. The value also lives in each node's frontmatter (``NodeWriter``); this column is the
cheap-to-query projection the chat-retrieval boost and the identity-capsule internal slice read
(T3 consumers).

Rule-1 clean: ``interiority`` is derived from the store frontmatter, so ``POST /admin/reindex``
repopulates it from the graph store — nothing here is source-of-truth. Nullable + no backfill:
existing rows stay ``NULL`` until the prod ``reprocess-all-from-raw`` (T5) re-derives them from raw
(ADR-055 §4 / P10); entity-hub nodes never carry it (only content nodes are stamped). Additive and
reversible.
"""

from __future__ import annotations

from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE nodes ADD COLUMN interiority text")


def downgrade() -> None:
    op.execute("ALTER TABLE nodes DROP COLUMN IF EXISTS interiority")
