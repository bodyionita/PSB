"""M0 initial schema — full data model (02-data-model.md).

Revision ID: 001
Revises:
Create Date: 2026-07-12

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. Ships the complete schema in
one revision — derived index (notes, chunks) + operational state (everything else).
Downgrades for the derived index are best-effort; the real recovery path is
POST /admin/reindex from the vault (02-data-model §5), not a schema rollback.
"""

from __future__ import annotations

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extensions: pgvector for embeddings; pgcrypto guarantees gen_random_uuid().
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- Derived index (rebuildable from vault) ---
    op.execute(
        """
        CREATE TABLE notes (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            vault_path      text UNIQUE NOT NULL,
            title           text,
            plane           text,
            planes          text[] NOT NULL DEFAULT '{}',
            tags            text[] NOT NULL DEFAULT '{}',
            source          text,
            source_ref      text,
            content_hash    text NOT NULL,
            note_created_at timestamptz,
            indexed_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX notes_plane_idx ON notes (plane)")
    op.execute("CREATE INDEX notes_planes_gin ON notes USING gin (planes)")
    op.execute("CREATE INDEX notes_tags_gin ON notes USING gin (tags)")

    op.execute(
        """
        CREATE TABLE chunks (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            note_id     uuid NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
            chunk_index int NOT NULL,
            content     text NOT NULL,
            embedding   vector(1536),
            UNIQUE (note_id, chunk_index)
        )
        """
    )
    # HNSW cosine index for similarity search (embedding dim fixed at 1536, ADR-004).
    op.execute(
        "CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )

    # --- Operational state (not rebuildable) ---
    op.execute(
        """
        CREATE TABLE captures (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind       text NOT NULL,
            status     text NOT NULL,
            raw_text   text,
            audio_path text,
            note_paths text[] NOT NULL DEFAULT '{}',
            error      text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX captures_created_at_idx ON captures (created_at DESC)")

    op.execute(
        """
        CREATE TABLE connector_cursors (
            connector  text PRIMARY KEY,
            cursor     jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE agent_runs (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            agent         text NOT NULL,
            status        text NOT NULL,
            started_at    timestamptz NOT NULL DEFAULT now(),
            finished_at   timestamptz,
            model_used    text,
            fallback_used boolean NOT NULL DEFAULT false,
            summary       text,
            details       jsonb NOT NULL DEFAULT '{}'::jsonb,
            error         text
        )
        """
    )
    op.execute("CREATE INDEX agent_runs_started_at_idx ON agent_runs (started_at DESC)")

    op.execute(
        """
        CREATE TABLE summaries (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            period       text NOT NULL,
            period_start date NOT NULL,
            content      text NOT NULL,
            note_path    text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            UNIQUE (period, period_start)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE chat_sessions (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            title      text,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_model text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE chat_messages (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
            role       text NOT NULL,
            content    text NOT NULL,
            model      text,
            sources    jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX chat_messages_session_idx ON chat_messages (session_id, created_at)")

    op.execute(
        """
        CREATE TABLE auth_sessions (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            token_hash   text UNIQUE NOT NULL,
            user_agent   text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz,
            expires_at   timestamptz NOT NULL,
            revoked      boolean NOT NULL DEFAULT false
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app_settings (
            key        text PRIMARY KEY,
            value      jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    # Reverse dependency order. Derived-index drops are best-effort (ADR-011).
    for table in (
        "app_settings",
        "auth_sessions",
        "chat_messages",
        "chat_sessions",
        "summaries",
        "agent_runs",
        "connector_cursors",
        "captures",
        "chunks",
        "notes",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    # Extensions are left in place — other databases on the instance may rely on them.
