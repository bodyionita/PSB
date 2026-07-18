"""M9 T2 — `media` table (multi-modal media substrate).

Revision ID: 017
Revises: 016
Create Date: 2026-07-18

Hand-authored plain SQL (ADR-011). Creates the source-generic **`media`** table
([ADR-057](../../second-brain-docs/adr/057-multimodal-media-ingestion-substrate.md) §3): one row
per media item (photo/voice/video), tracking the raw file location + the **resumable derivation
state** (status + derived text + model used). It serves ad-hoc PWA photo captures now (M9, linked
by ``capture_id``) and the Instagram-DM connector's media at M9.5 (which adds a nullable
``message_id`` fk to ``connector_messages`` in its own migration — 02-data-model names the item
``connector_media``; the physical table is ``media`` because it is source-generic and serves
ad-hoc captures too).

Columns (pinned here per the 02-data-model contract + ADR-057 §3):

* ``kind`` — ``photo`` | ``voice`` | ``video``.
* ``source`` — the producing surface (``capture`` for ad-hoc; ``instagram`` at M9.5), mirroring
  the ``captures.source`` vocabulary; also the top level of the on-disk layout
  ``/srv/data/media/<source>/…`` (ADR-057 §3).
* ``capture_id`` — nullable fk → ``captures`` (``ON DELETE CASCADE``): the ad-hoc-capture link
  (M9). Connector media leave it NULL and link via ``message_id`` (added at M9.5).
* ``file_path`` — the raw file's path **relative to the media root** (``/srv/data/media/``); NULL
  for ``video`` (summary-only — the recorded ADR-057 §2 exception, never uploaded/kept server-side).
* ``thumb_path`` — optional small thumbnail path (video, ADR-057 §2/§7); NULL otherwise.
* ``mime_type`` — the file's content type, so ``GET /media/{id}`` streams it with the right header.
* ``status`` — the derivation lifecycle: ``pending`` → ``derived`` | ``unavailable`` (bounded
  retries exhausted → explicit placeholder downstream; ``unavailable`` is targeted-re-derivable
  because raw is kept — ADR-057 §3).
* ``derived_text`` — the derived-tier output (photo description / voice transcript / video summary),
  recomputable from kept raw (P10, except video summaries — the §2 exception).
* ``model_used`` — the VLM/STT model that produced ``derived_text`` (vision P10 audit).
* ``attempts`` — derivation attempt count (the bounded-retry hinge → ``unavailable``).
* ``error`` — last derivation error, human-readable (rule 7 — nothing silent).

Rule-1 note: media *files* are operational raw (never lost; backed up to R2 via the ADR-014
``/srv/data`` sync), and ``derived_text`` is derived-tier (re-derivable from raw). The row itself is
operational state backed up with the DB. Additive + reversible.
"""

from __future__ import annotations

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE media (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind         text NOT NULL,
            source       text NOT NULL,
            capture_id   uuid REFERENCES captures (id) ON DELETE CASCADE,
            file_path    text,
            thumb_path   text,
            mime_type    text,
            status       text NOT NULL DEFAULT 'pending',
            derived_text text,
            model_used   text,
            attempts     integer NOT NULL DEFAULT 0,
            error        text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Targeted re-derivation scans by status (`pending`/`unavailable` items — ADR-057 §3).
    op.execute("CREATE INDEX media_status_idx ON media (status)")
    # Fetch an ad-hoc capture's media for the capture/node surfaces (M9 T3/T4).
    op.execute("CREATE INDEX media_capture_id_idx ON media (capture_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS media")
