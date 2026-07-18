"""M9.6 T4 — capture -> Activity-run deep-link (`captures.run_id`).

Revision ID: 020
Revises: 019
Create Date: 2026-07-19

Hand-authored plain SQL (ADR-011). Adds a nullable ``captures.run_id`` so a capture points
**directly** at its most recent processing ``agent_runs`` id — the Activity-tab deep-link
([ADR-061](../../second-brain-docs/adr/061-composite-multi-part-capture.md) §10). The pipeline
stamps it at ``_process``/reorganize **run-start**, so the link resolves **while the capture is
processing** (composite multi-photo runs are the slowest — live-follow is the point), not only
after finish. Replaces the read-time correlated scan of ``agent_runs.details->>'capture_id'`` (an
unindexed per-row subquery) with a stored fk-shaped column: cheaper reads, live deep-link.

Nullable + no backfill: existing rows keep ``NULL`` until their next processing run stamps it (a
reindex/reprocess is unnecessary — the column is a UI convenience, a missing value just hides the
chip). No fk constraint (``agent_runs`` rows are operational and may be pruned; a dangling id
simply yields an empty Activity view, never an integrity error). Additive + reversible.
"""

from __future__ import annotations

from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE captures ADD COLUMN run_id uuid")


def downgrade() -> None:
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS run_id")
