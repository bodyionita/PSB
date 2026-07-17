"""M8 task 1 — `agent_run_logs` (live-log-tail store) + `agent_runs.trigger`.

Revision ID: 015
Revises: 014
Create Date: 2026-07-17

Hand-authored plain SQL (ADR-011). The observability foundation
([ADR-053](../../../second-brain-docs/adr/053-m8-ops-console-observability-build-decisions.md)
§1/§2/§5), all additive:

- ``agent_runs.trigger`` (``scheduled`` | ``manual``, default ``scheduled``) — set through the
  ambient ``_trigger`` contextvar the manual endpoint wraps a run in (no job-body change), so the
  merged Activity feed can file a hand-run job under *manual actions* vs a scheduled run under
  *agents/jobs* by **origin**, not table (§5). Existing rows backfill to ``scheduled`` via the
  default — correct, they were all scheduler/CLI-driven.
- ``agent_run_logs`` — the durable live-log-tail store backing ``GET /activity/runs/{id}/logs``
  (§1/§2). An ``app.*``/``INFO``+ logging handler tags records by the active run (a
  ``_current_run_id`` contextvar) into a bounded per-run in-memory buffer, which an async flusher
  persists here on a ~1s cadence + on finish. ``seq`` is a per-run ordinal (assigned in-process at
  emit time, monotonic, gaps allowed on overflow-elision) so the poll cursor (``?after_seq=``) is
  stable and ordering never depends on wall-clock ``ts``.

Both are **rebuildable operational state, not graph truth** (rule 1) — the store remains the source
of record; these are dropped and re-derived like the rest of the index. The downgrade drops the log
table (index first) then the column.
"""

from __future__ import annotations

from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_runs ADD COLUMN trigger text NOT NULL DEFAULT 'scheduled'")
    op.execute(
        """
        CREATE TABLE agent_run_logs (
            id      bigserial PRIMARY KEY,
            run_id  uuid NOT NULL REFERENCES agent_runs (id) ON DELETE CASCADE,
            seq     integer NOT NULL,
            ts      timestamptz NOT NULL DEFAULT now(),
            level   text NOT NULL,
            message text NOT NULL
        )
        """
    )
    # The poll reads `WHERE run_id = $1 AND seq > $2 ORDER BY seq` — a composite index serves both
    # the filter and the order, and makes the per-run seq lookups cheap. UNIQUE guards a double
    # flush of the same (run, seq) line (the flusher assigns seq in-process; belt-and-braces).
    op.execute("CREATE UNIQUE INDEX agent_run_logs_run_seq_idx ON agent_run_logs (run_id, seq)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS agent_run_logs_run_seq_idx")
    op.execute("DROP TABLE IF EXISTS agent_run_logs")
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS trigger")
