"""M4 task 2 — hybrid FTS leg: generated tsvector columns + GIN (02-data-model §Migration 008).

Revision ID: 008
Revises: 007
Create Date: 2026-07-14

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. M4 retrieval fuses a Postgres
full-text leg with the existing vector leg by RRF ([ADR-032](adr/032-prior-art-adoptions.md) §5).
The FTS leg mirrors the ADR-037 vector union — it runs over the SAME node universe: the per-chunk
text (``chunks.content``) ⊍ the derived entity profile text (``node_profiles.profile``) — so RRF
fuses like-for-like.

Each table gains a ``tsv`` column ``GENERATED ALWAYS AS (to_tsvector('english', <text>)) STORED``
plus a **GIN** index. Generated + store-derived: the value is a pure function of a column the
indexer/profile-refresh already writes, so ``POST /admin/reindex`` (and a full reprocess) restore
it for free — no writer touches ``tsv`` (Rule 1 clean, ADR-001). The ``'english'`` config matches
the asserted English corpus (02 §3): a non-English ``tsquery`` produces no matching lexemes, so the
FTS leg self-suppresses without any language-detect dependency (M4 kickoff grill).

Idempotent (``IF NOT EXISTS``) so a re-run over a partially-migrated DB is safe.
"""

from __future__ import annotations

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # chunks: FTS over the indexed chunk text (the vector leg's `content`).
    op.execute(
        "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
    )
    op.execute("CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv)")
    # node_profiles: FTS over the derived entity profile text (mirrors the ADR-037 profile
    # vector leg, so an entity hub is FTS-reachable by name just as it is vector-reachable).
    op.execute(
        "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', profile)) STORED"
    )
    op.execute("CREATE INDEX IF NOT EXISTS node_profiles_tsv_gin ON node_profiles USING gin (tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS node_profiles_tsv_gin")
    op.execute("ALTER TABLE node_profiles DROP COLUMN IF EXISTS tsv")
    op.execute("DROP INDEX IF EXISTS chunks_tsv_gin")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS tsv")
