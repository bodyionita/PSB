"""M9.6 T1 — composite multi-part capture (draft lifecycle + part ordinal).

Revision ID: 019
Revises: 018
Create Date: 2026-07-19

Hand-authored plain SQL (ADR-011). The **additive** schema half of composite multi-part capture
([ADR-061](../../second-brain-docs/adr/061-composite-multi-part-capture.md)): one capture becomes an
optional typed text body + an ordered list of media parts (0..N photos + <=1 voice), composed on a
server-side **draft** and organized in one blended pass. The storage substrate is already
multi-capable (``media.capture_id`` is a nullable, non-unique fk; ``node_media`` is many-to-many),
so this migration only adds what the four narrower single-part places need (ADR-061 Context):

* ``captures.text_body`` — the person's typed words on a composite capture (never-lose + the
  reassembly source; ``raw_text`` stays the cached assembled organize/replay source, ADR-061 §5).
  Nullable; single-modality captures leave it NULL.
* ``media.part_ordinal`` — the explicit **position** of a media item within its capture (ADR-061
  §6), so assembly is deterministic across reprocess and the §7 attribution indices map to the
  right ``media`` row even after a draft-time delete + re-add reorders arrival. Nullable: NULL for
  non-part media (connector media at M9.5) and legacy single-part capture media (which order by
  ``created_at``, unchanged).

The ``captures.status`` gains a ``draft`` value and ``captures.kind`` a ``composite`` value; both
columns are free ``text`` (no enum/check), so no DDL is needed for the new vocabulary — only the
partial unique index below, which enforces the **one active draft at a time** invariant (ADR-061
§3) at the DB level: at most one ``captures`` row may be in ``status='draft'``.

Additive + reversible; existing rows are untouched (NULL ``text_body``/``part_ordinal``).
"""

from __future__ import annotations

from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE captures ADD COLUMN text_body text")
    op.execute("ALTER TABLE media ADD COLUMN part_ordinal integer")
    # One active draft at a time (ADR-061 §3): a partial unique index on the constant `status`
    # value restricted to draft rows means at most one row can be `status='draft'`. A second
    # `POST /capture/draft` resumes the existing draft rather than opening a new one; this index is
    # the DB backstop should the service ever race.
    op.execute(
        "CREATE UNIQUE INDEX captures_single_active_draft "
        "ON captures (status) WHERE status = 'draft'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS captures_single_active_draft")
    op.execute("ALTER TABLE media DROP COLUMN IF EXISTS part_ordinal")
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS text_body")
