"""M6 task 1 — `chat_distill_state` watermark + `captures.source_ref` (chat-distiller).

Revision ID: 013
Revises: 012
Create Date: 2026-07-16

Hand-authored plain SQL (ADR-011). Two additive changes for the chat-distiller
([ADR-048](../../second-brain-docs/adr/048-m6-chat-distiller-build-decisions.md)):

* **`chat_distill_state`** — one row per distilled chat session holding a message-timestamp
  **watermark** (ADR-048 §5). A distiller run processes only messages after ``last_message_at``,
  so re-runs (crash recovery, manual-then-nightly, a reopened thread) are idempotent and a no-op
  with no new activity. Idle-eligibility itself is derived live from ``max(chat_messages.created_at)``
  — this table is purely the delta cursor. FK ``ON DELETE CASCADE`` so deleting a session drops its
  cursor; ``run_id`` is a plain nullable uuid (the last distiller ``agent_runs`` id — informational,
  not a hard reference).
* **`captures.source_ref`** — a nullable locator column mirroring ``nodes.source_ref`` (02-data-model).
  An **endorsed** chat candidate materializes a ``captures`` row (``source=chat``) whose
  ``source_ref`` is the originating **chat-session id** (ADR-048 §1), so the chat→capture→node chain
  is traceable for the M6 audit/remove surfaces without embedding node ids in chat state. NULL for
  the pre-existing web/voice/MCP captures (unchanged).

Both are additive + reversible: the downgrade drops the table then the column. Neither touches
rebuildable index data — ``chat_distill_state`` is a re-derivable cursor and ``source_ref`` is
operational provenance.
"""

from __future__ import annotations

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE chat_distill_state (
            session_id      uuid PRIMARY KEY REFERENCES chat_sessions (id) ON DELETE CASCADE,
            last_message_at timestamptz NOT NULL,
            distilled_at    timestamptz NOT NULL DEFAULT now(),
            run_id          uuid
        )
        """
    )
    op.execute("ALTER TABLE captures ADD COLUMN source_ref text")


def downgrade() -> None:
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS source_ref")
    op.execute("DROP TABLE IF EXISTS chat_distill_state")
