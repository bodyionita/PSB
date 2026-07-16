"""M6 task 4 — `captures.removed_at` (tombstone) + `chat_auto_recorded` registry (one-tap remove).

Revision ID: 014
Revises: 013
Create Date: 2026-07-16

Hand-authored plain SQL (ADR-011). Two additive changes for the chat-distiller's audit/remove
surfaces ([ADR-048](../../second-brain-docs/adr/048-m6-chat-distiller-build-decisions.md) §11/§12):

* **`captures.removed_at`** — a nullable tombstone. One-tap remove of a chat-distilled node soft-
  deletes it: the node file is git-rm'd (history kept), its `nodes`/`chunks`/`edges` DB rows are
  deleted, and its backing capture row is tombstoned by stamping ``removed_at`` (§11). A non-null
  ``removed_at`` is **replay-excluded** — ``reprocess-all`` skips the capture so a deliberately
  removed memory can't resurrect (else the retained raw would rebuild it). Operational state (a
  user decision), not derived — never touched by a reprocess reset.

* **`chat_auto_recorded`** — the registry the chat-scoped "recently auto-recorded" audit list reads
  (§12 / 03-api ``GET /chat/auto-recorded``). One row per **auto-endorsed** distiller candidate
  (``capture_id`` → the ``source=chat`` capture the endorsed branch materializes), carrying the
  coarse ``salience`` tag for feed ranking. Its **existence marks a memory as auto-recorded** — the
  distinction the ADR draws: the list + the remove affordance are **auto-endorsed only**; an
  *agree-from-review* memory is user-vetted and materializes the same ``source=chat`` capture but
  writes **no** ``chat_auto_recorded`` row, so it stays out of this surface (§11). ``capture_id``
  FKs the capture ``ON DELETE CASCADE`` (raw is never deleted, so the row simply persists across a
  remove — the audit list filters on ``captures.removed_at IS NULL``). Provenance, not derived
  index: a reprocess replays captures through the organizer, not chat sessions, so it never
  re-mints these rows — they must survive (like the preserved ``stance-candidate`` items, §7).

Both are additive + reversible: the downgrade drops the table then the column.
"""

from __future__ import annotations

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE captures ADD COLUMN removed_at timestamptz")
    op.execute(
        """
        CREATE TABLE chat_auto_recorded (
            capture_id  uuid PRIMARY KEY REFERENCES captures (id) ON DELETE CASCADE,
            salience    text,
            recorded_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chat_auto_recorded")
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS removed_at")
