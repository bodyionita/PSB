"""M3 task 6 — derived entity profiles (node_profiles).

Revision ID: 006
Revises: 005
Create Date: 2026-07-14

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. Adds the storage for the
**derived entity profiles** (ADR-030 §4 / ADR-032 / ADR-034): the "who/what is X now"
summary regenerated nightly by the profile-refresh job (task 6) from an entity's 1-hop
neighborhood, served by ``GET /nodes/{id}``.

The profile lives in its own table rather than a ``nodes`` column because it is a
**derived tier** with a different lifecycle from the index row: 02-data-model §3 leaves the
storage "an implementation choice within the derived tier", and a dedicated table keeps the
indexer (the sole writer of ``nodes``/``chunks``) uncoupled from the profile-refresh job.
Fully rebuildable — a wiped ``node_profiles`` is repopulated by the next profile-refresh run
(a reindex does not touch it). Columns:

  * ``tier`` — the ADR-034 evidence tier (``stub``/``snapshot``/``full``), by graph degree;
  * ``profile`` — the rendered categorized observation lines served to the UI;
  * ``observations`` — the structured lines + their supporting node ids (citation discipline,
    rebuildability);
  * ``neighborhood_hash`` — a fingerprint of the 1-hop neighborhood the profile was built
    from, so the job SKIPS regeneration when nothing changed (idempotency + caps LLM spend);
  * ``embedding`` — the profile embedding (ADR-030 §4 "embedded"); populated so an entity can
    later surface in search/similarity by its current summary (wiring is a follow-up).
"""

from __future__ import annotations

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE node_profiles (
            node_id           uuid PRIMARY KEY REFERENCES nodes (id) ON DELETE CASCADE,
            tier              text NOT NULL,
            profile           text NOT NULL,
            observations      jsonb NOT NULL DEFAULT '[]'::jsonb,
            neighborhood_hash text NOT NULL,
            embedding         vector(768),
            refreshed_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    # Derived tier — the recovery path is a profile-refresh run, not a schema rollback.
    op.execute("DROP TABLE IF EXISTS node_profiles")
