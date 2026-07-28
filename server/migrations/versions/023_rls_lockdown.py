"""Deny-all lockdown of the `public` schema against the Supabase Data API.

Revision ID: 023
Revises: 022
Create Date: 2026-07-28

Hand-authored plain SQL (ADR-011).

**Why.** On the hosted Supabase project the `public` schema was reachable through PostgREST
with the *publishable* `anon` key — a key that is public by design — and Supabase's stock
grants gave `anon` and `authenticated` full `SELECT/INSERT/UPDATE/DELETE` on every table.
Verified live: an unauthenticated `GET /rest/v1/alembic_version` returned a row. That put
the whole graph (`nodes`, `edges`, `chunks`), the chat history, and the bearer tokens in
`mcp_tokens` / `auth_sessions` one public key away from being read *or wiped*.

**Posture.** Braindan is single-tenant and nothing goes through PostgREST: the server owns
its own auth and talks to Postgres directly over asyncpg as the table owner (`postgres`,
which carries `BYPASSRLS`). So the correct answer is not per-row policies keyed on
`auth.uid()` — there are no Supabase Auth users — it is **deny-all**:

* RLS enabled with **zero policies**, so every non-`BYPASSRLS` role sees nothing;
* the `anon` / `authenticated` grants revoked outright, so the tables are not even
  addressable;
* the schema-level default privileges revoked, so the next migration that adds a table does
  not silently reopen the hole (this is the part that makes the fix durable);
* `capture_interactions` switched to `security_invoker`, since a `SECURITY DEFINER` view
  runs as its creator and would have read straight through the RLS above.

The server is unaffected — an owner with `BYPASSRLS` is not subject to RLS, and its grants
are untouched. Re-enabling the Data API later means an explicit `GRANT` plus real policies,
which is the intended friction.

Idempotent and safe on a plain-Postgres dev database: `anon` / `authenticated` /
`supabase_admin` do not exist there, so every role-dependent statement is guarded on
`pg_roles`. Reversible, though `downgrade()` deliberately restores the *insecure* state only
because Alembic expects symmetry — see the warning there.
"""

from __future__ import annotations

from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


# The Supabase API roles. Absent on a local dev Postgres, hence every use is role-guarded.
_API_ROLES = "anon, authenticated"


def upgrade() -> None:
    # 1. RLS on every table in `public`. No policies == deny-all for anything without
    #    BYPASSRLS. Not FORCE: the owner (`postgres`, which the server connects as) must keep
    #    reading through it.
    op.execute(
        """
        DO $$
        DECLARE t record;
        BEGIN
            FOR t IN
                SELECT c.relname
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity
            LOOP
                EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.relname);
            END LOOP;
        END $$;
        """
    )

    # 2. Drop the stock grants, and 3. the default privileges that would re-grant them to
    #    every table a future revision creates. Without step 3 this migration fixes today's
    #    tables and quietly leaks tomorrow's.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                RAISE NOTICE 'no anon role - not a Supabase database, skipping grant lockdown';
                RETURN;
            END IF;

            REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM {_API_ROLES};
            REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {_API_ROLES};
            REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {_API_ROLES};

            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                REVOKE ALL ON TABLES    FROM {_API_ROLES};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                REVOKE ALL ON SEQUENCES FROM {_API_ROLES};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                REVOKE ALL ON FUNCTIONS FROM {_API_ROLES};

            -- Drops the *named* schema grants only. `public` also carries `USAGE` for the
            -- `PUBLIC` pseudo-role, which these roles keep inheriting, so this is tidying
            -- rather than a lock: the table grants above plus RLS are what actually deny
            -- access. Revoking from `PUBLIC` outright is deliberately not done here — it
            -- reaches well beyond the API roles. Disable the Data API in project settings
            -- if you want the belt-and-braces kill switch.
            REVOKE USAGE ON SCHEMA public FROM {_API_ROLES};
        END $$;
        """
    )

    # Supabase also registers default privileges under `supabase_admin`. Those only bite for
    # objects *it* creates (Alembic runs as `postgres`), and `postgres` may not be a member —
    # so this is best-effort by design, not a silent failure.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_admin') THEN
                RETURN;
            END IF;
            ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
                REVOKE ALL ON TABLES    FROM {_API_ROLES};
            ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
                REVOKE ALL ON SEQUENCES FROM {_API_ROLES};
            ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
                REVOKE ALL ON FUNCTIONS FROM {_API_ROLES};
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'not a member of supabase_admin; its default privileges left as-is';
        END $$;
        """
    )

    # 4. A SECURITY DEFINER view executes as its creator (`postgres`, BYPASSRLS) and would
    #    have handed out `agent_runs` regardless of step 1. `security_invoker` makes it run as
    #    the caller, so the deny-all applies through the view too.
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.capture_interactions') IS NOT NULL THEN
                ALTER VIEW public.capture_interactions SET (security_invoker = true);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Restore the pre-023 state.

    **This re-exposes `public` to the Data API via the publishable key.** It exists so the
    revision is reversible, not because reversing it is ever a good idea — if you need it,
    you almost certainly want to disable the Data API in project settings instead.
    """
    op.execute(
        """
        DO $$
        DECLARE t record;
        BEGIN
            FOR t IN
                SELECT c.relname
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity
            LOOP
                EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t.relname);
            END LOOP;
        END $$;
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                RETURN;
            END IF;
            GRANT USAGE ON SCHEMA public TO {_API_ROLES};
            GRANT ALL ON ALL TABLES    IN SCHEMA public TO {_API_ROLES};
            GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {_API_ROLES};
            GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO {_API_ROLES};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES    TO {_API_ROLES};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {_API_ROLES};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO {_API_ROLES};
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.capture_interactions') IS NOT NULL THEN
                ALTER VIEW public.capture_interactions SET (security_invoker = false);
            END IF;
        END $$;
        """
    )
