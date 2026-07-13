"""M2 embeddings resize (768) + semantic relatedness graph.

Revision ID: 004
Revises: 003
Create Date: 2026-07-13

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. M2 switches the embedder to
self-hosted ``nomic-embed-text-v1.5`` (768-dim) via Ollama ([ADR-022]) and materializes the
semantic relatedness graph ([ADR-023]). Concretely this revision:

  * resizes the (still-empty) ``chunks.embedding`` column from ``vector(1536)`` → ``vector(768)``
    — near-zero cost because the M1 index step was a no-op stub (``notes``/``chunks`` are empty);
  * adds ``notes.embedding vector(768)`` — the note-level vector (mean-pool of the note's chunk
    vectors) that powers ``note_links`` k-NN ([ADR-023]);
  * creates ``note_links`` — directional, scored semantic edges (each note's own top-K above the
    tuned floor), distinct from the co-capture ``related:`` frontmatter;
  * recreates the HNSW cosine indexes at the new dimension.

The HNSW indexes must be dropped before the type change (a vector index binds the column's
dimension) and recreated after. Derived-index downgrades are best-effort (ADR-011); the real
recovery path is ``POST /admin/reindex`` from the vault, not a schema rollback.
"""

from __future__ import annotations

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The HNSW index binds the column dimension, so drop it before resizing the type.
    op.execute("DROP INDEX IF EXISTS chunks_embedding_hnsw")

    # Resize the empty embedding column 1536 → 768 (ADR-022). No rows ⇒ no data to recast.
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(768)")

    # Note-level vector = mean-pool of the note's chunk embeddings (ADR-023); powers note_links.
    op.execute("ALTER TABLE notes ADD COLUMN embedding vector(768)")

    # Semantic relatedness graph (ADR-023): directional, scored edges. Rebuildable from
    # embeddings (same durability tier as notes/chunks), recomputed nightly. Distinct from the
    # co-capture `related:` frontmatter — this is *topical* relatedness.
    op.execute(
        """
        CREATE TABLE note_links (
            note_id         uuid NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
            related_note_id uuid NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
            score           real NOT NULL,
            PRIMARY KEY (note_id, related_note_id)
        )
        """
    )

    # HNSW cosine indexes at the new 768 dimension (embedding_dim is a setting, ADR-022).
    op.execute(
        "CREATE INDEX chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX notes_embedding_hnsw ON notes "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    # Best-effort reverse (derived index, ADR-011). Reverses upgrade in dependency order.
    op.execute("DROP INDEX IF EXISTS notes_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS chunks_embedding_hnsw")
    op.execute("DROP TABLE IF EXISTS note_links")
    op.execute("ALTER TABLE notes DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(1536)")
    op.execute(
        "CREATE INDEX chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
