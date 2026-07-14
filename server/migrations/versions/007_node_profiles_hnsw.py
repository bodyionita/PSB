"""M3 task 10 — HNSW index on node_profiles.embedding (profile-embedding-in-search).

Revision ID: 007
Revises: 006
Create Date: 2026-07-14

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. ADR-037 wires the derived
profile embedding into search — ``search_chunks`` gains a second per-profile vector leg over
``node_profiles.embedding``, unioned best-per-node with the chunk leg. This adds the ANN index
that leg needs, mirroring ``chunks_embedding_hnsw`` (migration 001/004): HNSW, cosine ops,
default build params. Without it the profile leg is a sequential scan — fine at today's tiny
profile count, but this keeps the two legs at plan parity as entities grow.

Idempotent (``IF NOT EXISTS``) so a re-run over a partially-migrated DB is safe.
"""

from __future__ import annotations

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS node_profiles_embedding_hnsw ON node_profiles "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS node_profiles_embedding_hnsw")
