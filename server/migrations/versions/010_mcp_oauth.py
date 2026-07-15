"""M5 task 3 — MCP OAuth 2.1 authorization-server tables.

Revision ID: 010
Revises: 009
Create Date: 2026-07-15

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. Stands up the storage for the
self-hosted OAuth 2.1 authorization server (ADR-046 §2) that gates the MCP surface — three
tables, all operational state (never the graph store, rule 1):

  * ``mcp_oauth_clients`` — dynamically registered connectors (RFC 7591 open DCR). A client is
    inert on its own (registration grants nothing); ``metadata`` holds its ``redirect_uris`` +
    ``client_name`` + auth method. Public (PKCE) clients carry no secret →
    ``client_secret_hash`` NULL.
  * ``mcp_auth_codes`` — short-lived, single-use, PKCE-bound authorization codes. The code is a
    credential, so only its HMAC hash is stored (``code_hash`` pk — same discipline as
    ``auth_sessions.token_hash``); ``consumed_at`` enforces one-time use atomically; ``expires_at``
    is very short (OAuth 2.1). Storing them in the DB (not memory) makes single-use consumption
    survive a restart and stay correct under concurrency.
  * ``mcp_tokens`` — opaque access + refresh tokens, HMAC-hashed like sessions (plaintext only
    ever reaches the connector once). ``kind`` = ``access`` (~1h) | ``refresh`` (long-lived,
    sliding via rotation); ``revoked_at`` powers the M5 **revoke-all** switch (a single UPDATE
    over every live row). ``resource`` records the RFC 8707 audience the token was minted for.

All fully rebuildable operational state — a DB restore repopulates them; a connector re-runs the
OAuth flow if they are lost. FK cascades keep codes/tokens tied to their client.
"""

from __future__ import annotations

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mcp_oauth_clients (
            client_id          text PRIMARY KEY,
            client_secret_hash text,
            metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mcp_auth_codes (
            code_hash             text PRIMARY KEY,
            client_id             text NOT NULL
                                      REFERENCES mcp_oauth_clients (client_id) ON DELETE CASCADE,
            redirect_uri          text NOT NULL,
            code_challenge        text NOT NULL,
            code_challenge_method text NOT NULL DEFAULT 'S256',
            scope                 text NOT NULL DEFAULT '',
            resource              text,
            expires_at            timestamptz NOT NULL,
            consumed_at           timestamptz,
            created_at            timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mcp_tokens (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id    text NOT NULL
                             REFERENCES mcp_oauth_clients (client_id) ON DELETE CASCADE,
            token_hash   text UNIQUE NOT NULL,
            kind         text NOT NULL,
            scope        text NOT NULL DEFAULT '',
            resource     text,
            expires_at   timestamptz NOT NULL,
            revoked_at   timestamptz,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_used_at timestamptz
        )
        """
    )
    # The revoke-all switch + refresh validation scan tokens by client and liveness; the code GC /
    # single-use path hits code_hash (the pk). One helper index for the client-scoped token sweeps.
    op.execute("CREATE INDEX mcp_tokens_client_idx ON mcp_tokens (client_id)")


def downgrade() -> None:
    # Operational state — recovery is a re-registration + re-auth, not a schema rollback.
    for table in ("mcp_tokens", "mcp_auth_codes", "mcp_oauth_clients"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
