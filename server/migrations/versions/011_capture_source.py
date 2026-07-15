"""M5 task 4 — capture `source` column (MCP-tagged captures).

Revision ID: 011
Revises: 010
Create Date: 2026-07-15

Hand-authored plain SQL (ADR-011). Adds a nullable ``source`` column to ``captures`` so a capture
can carry its origin surface (``web`` default | ``mcp`` here | later ``telegram``/``slack``) —
[ADR-046](adr/046) §4 / 02-data-model §capture. The pipeline stamps it onto the node frontmatter
``source:`` (falling back to the capture *kind* — ``text``/``voice`` — when unset, preserving the
pre-M5 web behaviour) so an MCP-driven capture is distinguishable in the graph + activity feed.

Nullable + no backfill: existing rows keep ``NULL`` and continue to render their kind as the node
source, exactly as before — additive and reversible.
"""

from __future__ import annotations

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE captures ADD COLUMN source text")


def downgrade() -> None:
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS source")
