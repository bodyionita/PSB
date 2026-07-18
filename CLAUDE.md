# CLAUDE.md — Personal Second Brain (code monorepo)

**Read `../second-brain-docs/` first** (order: README → 00…09 → adr/). The docs repo is
the contract; this file only enforces it. Do not contradict an ADR without writing a new
one in the docs repo.

**Every session follows the [session protocol](../second-brain-docs/09-session-protocol.md):**
**planning/replanning** sessions run `/grilling` first, record decisions to docs, and
**pause before implementation**; **implementation** sessions build against the approved plan
(no grilling), pausing between major tasks. Hit an unrecorded decision mid-implementation →
stop and replan, don't decide inline. **Commit + push docs at every pause**; commit code
freely while implementing but **push code only when the user asks**.

> **Pivot history (2026-07-13, ADR-026–029; rename landed with M3, 2026-07-14):** the design
> pivoted to a **typed mind graph** — graph store of nodes/edges, Obsidian removed, organizer
> as single writer, MCP peer surface. The M3 code rename (vault→graph store, notes→nodes,
> `VAULT_PATH`→`GRAPH_STORE_PATH`, `notes`/`note_links`→`nodes`/`edges`) is **done** —
> never introduce "note"/"vault" in new work (superseded ADRs and old logs excepted).

## Hard rules

1. **The graph store is truth** (ADR-001 + ADR-026). Postgres holds only rebuildable
   index (`nodes`/`chunks`/`edges`) + operational state; `POST /admin/reindex` must always
   be able to restore search/traverse from the store. Derived similarity lives in the DB
   only — never written into files.
2. **Never lose input.** Raw audio/text/sessions/connector items are persisted before any
   model call and never deleted by pipeline code. Model failures degrade (`inbox/` fallback
   node), they never drop data.
2b. **The organizer is the single writer of graph structure** (ADR-028). Every surface —
   REST, MCP, connectors, chat distillation — funnels node/edge creation through it;
   vocabulary is governed (ADR-027), stance is gated (ADR-029), never guessed.
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
11. **Secrets never touch git or the agent** ([09-session-protocol.md](../second-brain-docs/09-session-protocol.md)
    "Security & secrets discipline"). No private key, password, API token, session secret
    or connection string is ever pasted into chat, echoed by the agent, or written to a
    tracked file. Real values live only in `deploy/.env` on the VPS (gitignored), the
    process environment, or GitHub Actions secrets; only `.env.example` placeholders are
    committed. Enforced by `.githooks/pre-commit` (enable once: `git config core.hooksPath
    .githooks`) + a gitleaks CI job. `--no-verify` is forbidden for secret material; a
    leaked secret is treated as compromised and rotated immediately.
12. **LLMs classify, code computes** ([ADR-056](../second-brain-docs/adr/056-temporal-correctness-date-tokens.md)).
    No LLM-emitted arithmetic, date, or other numerical *derivation* is ever stored. The
    model emits symbolic classifications ("10 days ago" → `{kind: relative_days, offset:
    -10}`); deterministic code (stdlib `datetime`, plain Python — the temporal engine is
    deliberately dependency-free so the web mirror stays byte-identical) performs all
    computation against stored anchors — never wall-clock in replayable paths (P10).
    Unresolvable input degrades (stays prose / files review), it is never guessed.
    Binding on all pipelines, connectors, and agents.

## Conventions

- Python 3.12, `pathlib`, store paths `/`-separated relative to `GRAPH_STORE_PATH`.
  UTC timestamps in DB; `TZ` only for scheduling and store-facing formatting.
- **Vocabulary (ADR-026):** nodes, edges, the graph, the graph store — never "note"/"vault"
  in new code, endpoints, or docs once M3 lands (superseded ADRs and old logs excepted).
- Web: React + Vite + TS strict, TanStack Query for server state, framer-motion for all
  motion (respect `prefers-reduced-motion`), feature-folder structure, design-system
  primitives in `ui/`.
- Lint/format: ruff (server), eslint+prettier (web). CI must stay green per directory.

## When extending

- Feature in the v2 backlog (docs 08)? Confirm scope before building.
- New architectural choice? New ADR in `../second-brain-docs/adr/` first.
- Behavior change? Update the relevant doc in the same change set.
