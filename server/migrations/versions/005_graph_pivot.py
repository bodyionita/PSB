"""M3 graph pivot — nodes/edges/review_queue replace the note model.

Revision ID: 005
Revises: 004
Create Date: 2026-07-13

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. THE MIND-GRAPH PIVOT
(ADR-026/027) as grilled to build-ready (ADR-030/031/032; DDL contract in
02-data-model.md v3.2 §3). Fresh start — the old vault is archived, no data is
migrated. Concretely this revision:

  * drops the note-model derived tables (``note_links``, ``chunks``, ``notes``);
  * creates ``nodes`` — one row per indexed node file, keyed by the frontmatter ``id``
    (no column default: paths are projections, the indexer always supplies the id),
    with the entity substrate (``aliases`` GIN — this *is* the alias index, ADR-030 —
    plus ``disambig``), the ``occurred`` range (ADR-031), ``organizer_version``
    (retrofit targeting) and ``merged_into`` (tombstone marker; plain uuid, no self-FK,
    so reindex insertion order never matters);
  * recreates ``chunks`` re-keyed ``note_id`` → ``node_id``;
  * creates ``edges`` — both origins in one table (``canonical`` from frontmatter,
    ``derived`` from embeddings), one ``score`` column serving both (confidence /
    cosine, ADR-031), ``since``/``until`` validity window (ADR-030/032 — ``until``
    closes a superseded relation; invalidate, never delete), plus the ``dst_id``
    reverse index that merge relies on (ADR-030 §5);
  * creates ``review_queue`` — kind-generic, pulled forward from M6 (ADR-030 §3);
    operational state, NOT rebuildable;
  * renames ``captures.note_paths`` → ``node_paths`` (values on old rows keep the
    pre-pivot vault paths as historical locators — captures are never-lose).

``summaries`` is retired at the pivot but the table is kept until M10 replaces it (no
new writers). Derived-index downgrades are best-effort (ADR-011); the real recovery
path is ``POST /admin/reindex`` from the graph store, not a schema rollback. NB the
downgrade also drops ``review_queue`` — operational state: pending review items are
recoverable only from the nightly pg_dump (02-data-model §5), so don't downgrade past
005 on a live database casually.
"""

from __future__ import annotations

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Note model out (dependency order). Fresh start: nothing is copied. ---
    op.execute("DROP TABLE IF EXISTS note_links")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS notes")

    # --- Derived graph index (rebuildable from the graph store) ---
    op.execute(
        """
        CREATE TABLE nodes (
            id                uuid PRIMARY KEY,
            store_path        text UNIQUE NOT NULL,
            type              text NOT NULL,
            title             text,
            plane             text,
            planes            text[] NOT NULL DEFAULT '{}',
            tags              text[] NOT NULL DEFAULT '{}',
            aliases           text[] NOT NULL DEFAULT '{}',
            disambig          text,
            occurred_start    date,
            occurred_end      date,
            organizer_version text,
            merged_into       uuid,
            source            text,
            source_ref        text,
            content_hash      text NOT NULL,
            embedding         vector(768),
            node_created_at   timestamptz,
            indexed_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX nodes_type_idx ON nodes (type)")
    op.execute("CREATE INDEX nodes_plane_idx ON nodes (plane)")
    op.execute("CREATE INDEX nodes_planes_gin ON nodes USING gin (planes)")
    op.execute("CREATE INDEX nodes_tags_gin ON nodes USING gin (tags)")
    # The alias index (ADR-030 §1): entity resolution matches capture mentions here.
    op.execute("CREATE INDEX nodes_aliases_gin ON nodes USING gin (aliases)")
    # HNSW cosine (embedding_dim is a setting, ADR-022); powers derived-edge k-NN.
    op.execute(
        "CREATE INDEX nodes_embedding_hnsw ON nodes USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE chunks (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            node_id     uuid NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
            chunk_index int NOT NULL,
            content     text NOT NULL,
            embedding   vector(768),
            UNIQUE (node_id, chunk_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE edges (
            src_id uuid NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
            dst_id uuid NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
            rel    text NOT NULL,
            origin text NOT NULL,
            score  real,
            since  date,
            until  date,
            PRIMARY KEY (src_id, dst_id, rel, origin)
        )
        """
    )
    # Reverse index: inbound-edge lookup for merge rewrites + neighbor traversal.
    op.execute("CREATE INDEX edges_dst_idx ON edges (dst_id)")

    # --- Operational state (not rebuildable) ---
    op.execute(
        """
        CREATE TABLE review_queue (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind        text NOT NULL,
            payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
            excerpt     text,
            source      text,
            source_ref  text,
            status      text NOT NULL DEFAULT 'pending',
            resolution  jsonb,
            created_at  timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX review_queue_status_idx ON review_queue (status, created_at DESC)"
    )

    op.execute("ALTER TABLE captures RENAME COLUMN note_paths TO node_paths")


def downgrade() -> None:
    # Best-effort reverse to the 004 note-model schema (ADR-011); derived tables come
    # back empty — recovery is a reindex, not a rollback.
    op.execute("ALTER TABLE captures RENAME COLUMN node_paths TO note_paths")
    op.execute("DROP TABLE IF EXISTS review_queue")
    op.execute("DROP TABLE IF EXISTS edges")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS nodes")

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
            embedding       vector(768),
            note_created_at timestamptz,
            indexed_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX notes_plane_idx ON notes (plane)")
    op.execute("CREATE INDEX notes_planes_gin ON notes USING gin (planes)")
    op.execute("CREATE INDEX notes_tags_gin ON notes USING gin (tags)")
    op.execute(
        "CREATE INDEX notes_embedding_hnsw ON notes USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        """
        CREATE TABLE chunks (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            note_id     uuid NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
            chunk_index int NOT NULL,
            content     text NOT NULL,
            embedding   vector(768),
            UNIQUE (note_id, chunk_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )
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
