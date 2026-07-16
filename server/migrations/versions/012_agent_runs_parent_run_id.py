"""M5.5 task 1 — `agent_runs.parent_run_id` (pipeline parent/child run linkage).

Revision ID: 012
Revises: 011
Create Date: 2026-07-16

Hand-authored plain SQL (ADR-011). Adds a nullable self-referencing ``parent_run_id`` to
``agent_runs`` so a pipeline run (ADR-047) can open a **parent** row while each of its steps keeps
its **own child** row linked back to the parent. A bare job run — the standalone CLI / ``POST
/agents/{name}/run`` path — leaves it ``NULL`` exactly as before, so the existing per-job
observability is unchanged (ADR-047 §5, 01-architecture invariant 4).

Nullable + self-referencing FK (``ON DELETE SET NULL`` so purging a parent never orphans a child
row into a dangling reference) + an index for the child lookup. Additive and reversible: the
downgrade drops the index then the column.
"""

from __future__ import annotations

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agent_runs
            ADD COLUMN parent_run_id uuid
            REFERENCES agent_runs (id) ON DELETE SET NULL
        """
    )
    op.execute("CREATE INDEX agent_runs_parent_run_id_idx ON agent_runs (parent_run_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS agent_runs_parent_run_id_idx")
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS parent_run_id")
