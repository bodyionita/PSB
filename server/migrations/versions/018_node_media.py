"""M9 T4 — `node_media` link table (node ↔ media, ADR-060 §1).

Revision ID: 018
Revises: 017
Create Date: 2026-07-18

Hand-authored plain SQL (ADR-011). Creates the first-class **`node_media`** many-to-many link
([ADR-060](../../second-brain-docs/adr/060-node-media-linkage-and-voice-unification.md) §1): the
path from a node — the thing the user reads — to the media it was built from. `media` rows still
hang off *captures* (`media.capture_id`, migration 017); this table adds the node→media edge the
`GET /nodes/{id}.media[]` surface + the search/chat `media_kinds` glyphs read.

Design (ADR-060 §1–§4):

* A plain DB relationship, **not a graph edge** — it never appears in `edges`, traverse, the Map, or
  MCP. It is a *media attachment*.
* **`node_id`** — fk → `nodes` (``ON DELETE CASCADE``): keyed on the stable `nodes.id` (never
  `store_path`), matching `GET /nodes/{id}` addressing + surviving path churn (§3). The cascade
  reaps a link when its node row is deleted (reindex reconciliation / reprocess `TRUNCATE nodes
  CASCADE`) — the link is **derived-tier**, rebuilt on every content-node write (§3), so it has no
  independent durability.
* **`media_id`** — fk → `media` (``ON DELETE CASCADE``): a media row is the raw-truth anchor; the
  rebuild is keyed on it (delete this media's links, re-insert the current content nodes').
* **`PRIMARY KEY (node_id, media_id)`** — the unique-pair guard (§1): a merge repoint / a rebuild is
  ``ON CONFLICT DO NOTHING``-safe.

The `media_id` index serves the derived-tier rebuild (delete-by-media) + the merge repoint
(ADR-060 §4). The (node_id, media_id) pk already indexes the `node_id` lookup that
`GET /nodes/{id}.media[]` + the `media_kinds` join use. Additive + reversible.
"""

from __future__ import annotations

from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE node_media (
            node_id    uuid NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
            media_id   uuid NOT NULL REFERENCES media (id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (node_id, media_id)
        )
        """
    )
    # The derived-tier rebuild (delete this media's links) + the merge repoint (ADR-060 §4) scan by
    # media_id; the (node_id, media_id) pk already covers the node_id-keyed read surfaces.
    op.execute("CREATE INDEX node_media_media_id_idx ON node_media (media_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS node_media")
