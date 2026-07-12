# CLAUDE.md — Personal Second Brain (code monorepo)

**Read `../second-brain-docs/` first** (order: README → 00…09 → adr/). The docs repo is
the contract; this file only enforces it. Do not contradict an ADR without writing a new
one in the docs repo.

**Every session follows the [session protocol](../second-brain-docs/09-session-protocol.md):**
run `/grilling` before coding, record decisions to docs, **pause before implementation** and
between major tasks, and **commit + push docs at every pause**. Commit code freely while
implementing; **push code only when the user asks**.

## Hard rules

1. **Vault is truth** (ADR-001). Postgres holds only rebuildable index + operational
   state; `POST /admin/reindex` must always be able to restore search from the vault.
2. **Never lose input.** Raw audio/text/connector items are persisted before any model
   call and never deleted by pipeline code. Model failures degrade (Inbox fallback note),
   they never drop data.
3. **Provider boundary** (ADR-004). Only `server/…/providers/` may import vendor SDKs
   (`openai`, Agent SDK). Everything else depends on the registry interfaces. Every
   LLM/STT/embedding call goes through the registry; fallback resolution is recorded
   (`model_used`, `fallback_used`) — never swallowed.
4. **server/web separation is law** (ADR-006). No shared code, no cross-imports; the web
   client's only server knowledge is the API base URL + OpenAPI-generated types. Anything
   that would break a future 2-repo split is a rejected design.
5. **Layering (server).** Routers = validation + delegation. Services = business logic.
   One DB module owns connections. **Plain SQL via asyncpg — no ORM.** Schema changes are
   **Alembic** revisions authored as explicit SQL (`op.execute`/`op.create_table`), no ORM,
   no autogenerate ([ADR-011](../second-brain-docs/adr/011-alembic-migrations-plain-sql-no-orm.md));
   applied via `alembic upgrade head` in CI/provisioning, never in the request path.
   SQLAlchemy is a migration-only dependency and must not leak past `migrations/`.
6. **Idempotency everywhere.** Reindexing, agent reruns, summary reruns, capture retries
   are always safe: content hashes, upserts, cursors advanced only past materialized work.
7. **Everything visible** (vision P8). Agent/job work lands in `agent_runs` with a
   human-readable summary + details JSON. No bare `except: pass`; failures end runs as
   `failed` with context, they never crash the service.
8. **Async end-to-end** in request paths and jobs; blocking work goes through
   `asyncio.to_thread`.
9. **Config only via the settings module** (env-backed). No `os.environ` elsewhere; no
   hardcoded models, paths, dimensions, schedules or plane lists.
10. **Quality bar:** modular, no duplication, OOP where it clarifies (services,
    providers, connectors), type hints everywhere (Python) / strict TS (web). Follow the
    testing policy in docs 08 — pure logic unit-tested, fakes for services, no live
    APIs/LLMs in tests.

## Conventions

- Python 3.12, `pathlib`, vault paths stored `/`-separated relative to `VAULT_PATH`.
  UTC timestamps in DB; `TZ` only for scheduling and vault-facing formatting.
- Web: React + Vite + TS strict, TanStack Query for server state, framer-motion for all
  motion (respect `prefers-reduced-motion`), feature-folder structure, design-system
  primitives in `ui/`.
- Lint/format: ruff (server), eslint+prettier (web). CI must stay green per directory.

## When extending

- Feature in the v2 backlog (docs 08)? Confirm scope before building.
- New architectural choice? New ADR in `../second-brain-docs/adr/` first.
- Behavior change? Update the relevant doc in the same change set.
