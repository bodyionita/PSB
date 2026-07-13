# Braindan — your personal second brain

**A mind graph of your life, available anywhere, any time — to you and to your AI tools.** Talk or
type a thought and it's captured in seconds; the AI organizes it into **typed nodes** (memories,
people, ideas, conversations, insights) connected by **meaningful relations** — memories linked to
the people they involve, ideas to where they came from. Question it in chat, feed and query it from
any LLM over MCP, and walk it visually as a map of your own mind. The goal isn't storage — it's an
extension of memory and a reflection partner. Information → knowledge → understanding, compounding
over time.

Your memory lives as **plain Markdown you own** — a git-versioned **graph store** on your own
server, fully recoverable. Everything else (search index, edges, chat) is derived and rebuildable
from those files.

> **Single-user, self-hosted, private by design.** It runs on your own always-on VPS behind
> Cloudflare — no personal machine required, no third party holding your thoughts.

> **⚡ Status (2026-07-13): the mind-graph pivot.** M0–M2 shipped and run live at `braindan.cc` on
> the original *note-vault* model (capture → organize → semantic search, full durability). The
> design has now pivoted to the typed graph above
> ([ADR-026–029](../second-brain-docs/adr/026-graph-native-storage-obsidian-removed.md)) — Obsidian
> is gone from the architecture, and milestone **M3 rebuilds the core around typed nodes + edges**
> (fresh graph store; the old vault gets archived). Until M3 lands, the deployed system is the
> pre-pivot one described in the shipped-milestone rows below.

---

## What it does for you

- **🎙️ Frictionless capture.** One tap from your phone's lock screen — speak or type — and you're
  done in under 10 seconds. No titles, no types, no "which folder?". Voice is transcribed
  automatically (Groq → OpenAI Whisper fallback), any language in, English memory out.
- **🧠 The AI files it, not you.** Each capture becomes **typed, atomic nodes** — classified into
  the **planes of your life** (`Professional · Personal · Family · Friends · Health · Ideas`,
  configurable), tagged, **entity-resolved** (the "Alex" you mention becomes *the* Alex node) and
  linked with typed edges (`involves`, `about`, `led_to`, …). Can't place it? It lands in `inbox/`
  — never guessed. *(Typed graph lands at M3; the live system files atomic notes per plane.)*
- **🔎 Semantic search over everything.** Ask in your own words and find the right memory by
  meaning, not keywords — self-hosted `nomic` embeddings + pgvector, with plane filters and a
  read-only preview.
- **🕸️ A graph that reflects *meaning*.** Canonical typed edges written at ingest, plus derived
  similarity links recomputed nightly — the structure your thinking actually has.
- **💬 Chat over your whole memory.** *(M4)* Answers grounded in your nodes with `[n]` source
  citations, on the model you pick, with an honest "not in your memories" when it isn't there.
- **🔌 Your brain on MCP.** *(M5)* Query **and feed** the graph from Claude or any MCP client —
  same organizer, same discipline, one bearer token away.
- **📥 Conversations become memory.** *(M6)* In-app chats are distilled nightly — anchored on
  **your** stance, not the model's words; anything unclear waits in a review queue for your
  agree/disagree. *(M9)* Slack conversations flow in the same way (6-month default lookback).
- **🗺️ The map.** *(M7)* Point-and-click exploration: center on a person, fan out their
  constellation of memories, keep walking. Desktop-first.
- **🧭 Agents that reflect, not just summarize.** *(M8–M11)* An ops console with live job
  logs/schedules; a morning reflection agent over 1d/1w/1m/1y windows (push notifications); a
  life-manager for schedule/tasks/goals.
- **🔐 Yours, and unloseable.** Raw captures are persisted before any model call and never
  dropped; the graph store is git-versioned with fast-forward-only push, off-site WORM backups to
  R2, and a weekly integrity drill. Any component can burn down without memory loss.
- **🔁 Model independence with a preference.** Claude (Max subscription, via the Agent SDK) is the
  primary mind; automatic fallback to Nebius; embeddings self-hosted. Every model call goes through
  one provider registry, and every fallback is recorded, never silent.

## How it works

```
   Phone / Desktop  ──HTTPS──►  Hetzner VPS (always on, behind Cloudflare)
   PWA: capture, search,        ├─ Caddy (TLS, serves the PWA, proxies /api)
   chat, review, map,           ├─ FastAPI service — one service layer under two thin surfaces:
   activity, settings           │    REST (the PWA) + MCP (other LLMs, M5) — capture / organize /
                                │    index / search / traverse / distill pipelines
   Other LLMs ──MCP (M5)──►     ├─ Provider registry — Claude → Nebius (chat), Groq → OpenAI (STT),
                                │    self-hosted nomic via an ollama sidecar (embeddings)
                                ├─ Scheduler — ingestion + analysis in a nightly 03:00–05:00 window
                                │
                                ├─ GRAPH STORE (typed-node Markdown, THE source of truth)
                                │        └──git push──► private GitHub  + R2 WORM bundles
                                └─ Supabase Postgres + pgvector (derived nodes/edges index +
                                                                 operational state)
```

**The graph store is truth; the database is a cache.** Content only ever flows store → index,
never back. Drop every derived table and one `POST /admin/reindex` rebuilds search, edges and chat
grounding from the Markdown. The web client and server share nothing but the HTTP contract.

Full design lives in [`../second-brain-docs/`](../second-brain-docs/) — start with its
[README](../second-brain-docs/README.md), then [00-vision](../second-brain-docs/00-vision.md) and
[01-architecture](../second-brain-docs/01-architecture.md). The
[ADRs](../second-brain-docs/adr/) record the *why* behind every choice.

## Status & roadmap

Shipped in phases; every phase ends usable. See the task tracker in
[08-implementation-plan.md](../second-brain-docs/08-implementation-plan.md).

| Milestone | What it delivers | State |
|---|---|---|
| **M0** Foundations | VPS + PWA + auth + `/health`, deployed live at `braindan.cc` | ✅ accepted |
| **M1** Capture | Voice/text → organized atomic notes; full vault durability | ✅ accepted |
| **M2** Indexing & search | Embeddings, indexer, semantic `/search`, relatedness graph | ✅ accepted |
| **M3** Graph core — **the pivot** | Typed nodes + edges, entity resolution, vocabulary governance, fresh graph store | ⏳ next (needs its build-ready grilling) |
| **M4** Chat | Grounded `[n]`-cited chat + UI-editable model routing (plan carried from ADR-025) | ⏳ planned |
| **M5** MCP server | Query + store from any LLM | ⏳ planned |
| **M6** Chat-distiller | Stance-gated conversational ingestion + review queue | ⏳ planned |
| **M7** The map | Point-and-click neighborhood explorer | ⏳ planned |
| **M8** Ops console | Live job logs, manual triggers, schedules; activity tabs | ⏳ planned |
| **M9** Slack connector | Stance-gated Slack ingestion, 6-month lookback | ⏳ planned |
| **M10** Reflection agent | 1d/1w/1m/1y reflections + push notifications | ⏳ planned |
| **M11** Life-manager agent | Schedule / tasks / goals | ⏳ planned |

## Tech stack

- **Server** — Python 3.12 · FastAPI · asyncpg (plain SQL, **no ORM**) · Alembic migrations ·
  APScheduler · uv. Postgres + pgvector (Supabase).
- **Web** — React + Vite + TypeScript (strict) · TanStack Query · framer-motion · installable PWA
  (pnpm). Talks only to the HTTP API.
- **Infra** — Docker Compose on a Hetzner VPS · Caddy (single origin) · Cloudflare (TLS + DNS proxy)
  · GitHub Actions (lint/test/build + deploy) · Cloudflare R2 (off-site WORM backups).

## Layout ([ADR-006](../second-brain-docs/adr/006-monorepo-with-strict-server-web-decoupling.md))

```
second-brain/
├── server/     FastAPI service — capture, indexing, search, providers, durability jobs
├── web/        React + Vite + TS PWA — talks only to the HTTP API
├── deploy/     Dockerfiles, compose, Caddy, provision.sh
└── .github/    CI (per-directory path filters)
```

`server/` and `web/` share **nothing** but the HTTP contract in
[`03-api.md`](../second-brain-docs/03-api.md) — each builds and runs standalone.

## Quick start (local dev)

### Database
```bash
docker compose -f deploy/docker-compose.dev.yml up -d      # pgvector on localhost:5432
```

### Server
```bash
cd server
uv sync                                                    # or: python -m venv .venv && pip install -e ".[dev]"
cp .env.example .env                                       # then set API_PASSWORD_HASH (below)
uv run python scripts/hash_password.py 'your-dev-password' # prints the argon2 hash for .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload                       # http://localhost:8000/api/v1/health
uv run pytest                                              # unit tests (no live LLMs/DB required)
```

Semantic search needs embeddings: run an [ollama](https://ollama.com) sidecar with
`nomic-embed-text` pulled, or point `EMBEDDING_PROVIDER_ID` at a hosted alternative (see
`.env.example`).

### Web  (Node 24 + pnpm 9; run `corepack enable` once)
```bash
cd web
pnpm install
pnpm dev                                                   # http://localhost:5173
pnpm build                                                 # type-check (tsc) + production build
```

## Contributing / design rules

This repo **implements** the design in [`../second-brain-docs/`](../second-brain-docs/) — that repo
is the contract. Don't diverge from an ADR without writing a new one there first. The hard rules
(graph-store-is-truth, organizer-single-writer, provider boundary, plain SQL, idempotency,
everything-visible, secrets-never-in-git) live in [CLAUDE.md](CLAUDE.md).

## License

Source-available under the **PolyForm Noncommercial License 1.0.0** ([LICENSE.md](LICENSE.md)):
free for any noncommercial purpose, attribution required (keep the `Required Notice:` line).
**Commercial use requires a separate paid license** — see [COMMERCIAL.md](COMMERCIAL.md).
