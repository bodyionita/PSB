"""M1 capture interactions view (ADR-021).

Revision ID: 003
Revises: 002
Create Date: 2026-07-12

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. Adds a read-only VIEW that
flattens the ``agent_runs`` rows written by the capture pipeline (``agent = 'capture'``) into
readable columns for the Supabase dashboard / MCP and the future M4 activity feed. It owns no
data — it is a projection over ``agent_runs.details`` (the JSON shape written by
``CapturePipeline``: ``{capture_id, kind, stt, organize, nudge, timings_ms}``).
"""

from __future__ import annotations

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIEW capture_interactions AS
        SELECT
            id                                             AS run_id,
            details ->> 'capture_id'                       AS capture_id,
            details ->> 'kind'                             AS kind,
            details -> 'stt'  ->> 'provider'               AS stt_provider,
            (details -> 'stt' ->> 'fallback_used')::boolean AS stt_fallback,
            details -> 'organize' ->> 'model'              AS organize_model,
            (details -> 'organize' ->> 'inbox_fallback')::boolean AS inbox_fallback,
            fallback_used,
            status,
            error,
            started_at,
            (details -> 'timings_ms' ->> 'total')::int     AS duration_ms
        FROM agent_runs
        WHERE agent = 'capture'
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS capture_interactions")
