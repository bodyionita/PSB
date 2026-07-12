"""M1 capture follow-up columns (ADR-019).

Revision ID: 002
Revises: 001
Create Date: 2026-07-12

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. Adds the two nullable columns
that carry the single "dig deeper" nudge — `follow_up_question` is generated after a
successful organize; `follow_up_answer` is filled when the user answers, triggering Pass 2
(re-organize original + answer, replacing the capture's notes). Question-present +
answer-absent is the "nudge pending" signal; no new status column (02-data-model §3).
"""

from __future__ import annotations

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE captures ADD COLUMN follow_up_question text")
    op.execute("ALTER TABLE captures ADD COLUMN follow_up_answer text")


def downgrade() -> None:
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS follow_up_answer")
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS follow_up_question")
