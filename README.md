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

> **⚡ Status (2026-07-18):** the **graph-native system is live at `braindan.cc`** — M0 through
> **M8.2** shipped and accepted (typed graph core · grounded cited chat · MCP over OAuth 2.1,
> connector-verified on Claude *and* ChatGPT · stance-gated chat distiller · the map/Explore ·
> ops console · interiority + temporal correctness). **Next:** M9 (multi-modal ingestion
> foundation) + M9.5 (Instagram DM connector) — grilled to build-ready, see the
> [docs repo](../second-brain-docs/).

---

## What it does for you

- **🎙️ Frictionless capture.** One tap from your phone's lock screen — speak or type — and you're
  done in under 10 seconds. No titles, no types, no "which folder?". Voice is transcribed
  automatically (Groq → OpenAI Whisper fallback), any language in, English memory out. *(M9 adds
  photo capture — vision-described, screenshot-aware.)*
- **🧠 The AI files it, not you.** Each capture becomes **typed, atomic nodes** — classified into
  the **planes of your life** (`Professional · Personal · Family · Friends · Health · Ideas`,
  configurable), tagged, **entity-resolved** (the "Alex" you mention becomes *the* Alex node) and
  linked with typed edges (`involves`, `about`, `led_to`, …). Can't place it? It lands in `inbox/`
  — never guessed.
- **🔎 Hybrid search over everything.** Ask in your own words and find the right memory by meaning
  *and* keywords — self-hosted `nomic` embeddings + pgvector fused (RRF) with full-text search,
  recency-aware, profile-aware.
- **🕸️ A graph that reflects *meaning*.** Canonical typed edges written at ingest, plus derived
  similarity links recomputed nightly — the structure your thinking actually has.
- **💬 Chat over your whole memory.** Answers grounded in your nodes with `[n]` source citations,
  on the model you pick, with an honest "not in your memories" when it isn't there. Relative dates
  resolve correctly — "10 days ago" said in 2019 stays 2019 (["LLMs classify, code
  computes"](../second-brain-docs/adr/056-temporal-correctness-date-tokens.md)).
- **🔌 Your brain on MCP.** Query **and feed** the graph from Claude (mobile/web) or ChatGPT —
  same organizer, same discipline, behind a self-hosted **OAuth 2.1** flow with one-tap
  revoke-all.
- **📥 Conversations become memory.** In-app chats are distilled nightly — anchored on **your**
  stance, not the model's words; anything unclear waits in a review queue for your
  agree/disagree. *(M9.5)* Your **Instagram DM history** (10 years, photos/voice/video
  understood) flows in the same way; Slack follows at M12.
- **🗺️ The map.** Point-and-click exploration: center on a person, fan out their constellation of
  memories, keep walking — one Explore tab with search.
- **🧭 Agents that reflect, not just summarize.** An ops console with live job logs/schedules and
  a nightly pipeline (profiles, dedup sweep, graph-health, identity capsule); next up (M10–M11): a
  morning reflection agent (push notifications) and a life-manager for schedule/tasks/goals.
- **🔐 Yours, and unloseable.** Raw captures are persisted before any model call and never
  dropped; the graph store is git-versioned with fast-forward-only push, off-site WORM backups to
  R2, and a weekly integrity drill. Anything derived rebuilds from raw
  (`reprocess-all-from-raw`). Any component can burn down without memory loss.
- **🔁 Model independence with a preference.** Claude (Max subscription) is the primary mind;
  automatic fallback to Nebius; embeddings self-hosted. Every model call goes through one provider
  registry with UI-editable routing (provider ≠ model), and every fallback is recorded, never
  silent.

## How it works

```
   Phone / Desktop  ──HTTPS──►  Hetzner VPS (always on, behind Cloudflare)
   PWA: capture, chat,          ├─ Caddy (TLS, serves the PWA, proxies /api + /mcp)
   review, explore/map,         ├─ FastAPI service — one service layer under two thin surfaces:
   activity, settings           │    REST (the PWA) + MCP (other LLMs, OAuth 2.1) — capture /
                                │    organize / index / search / traverse / distill pipelines
   Other LLMs ──MCP──►          ├─ Provider registry — Claude → Nebius (chat), Groq → OpenAI (STT),
                                │    self-hosted nomic via an ollama sidecar (embeddings)
                                ├─ Scheduler — nightly + weekly pipelines (03:00–05:00 window)
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

Shipped in phases; every phase ends usable. The authoritative task tracker lives in
[08-implementation-plan.md](../second-brain-docs/08-implementation-plan.md).

| Milestone | What it delivers | State |
|---|---|---|
| **M0–M2** Foundations · capture · search | VPS + PWA + auth; voice/text capture; embeddings + semantic search (pre-pivot note model) | ✅ accepted |
| **M3** Graph core — **the pivot** | Typed nodes + edges, entity resolution, vocabulary governance, fresh graph store | ✅ accepted |
| **M4** Chat (+2 follow-ups) | Grounded `[n]`-cited chat; UI model routing (provider ≠ model); provider observability | ✅ accepted |
| **M5 / M5.5** MCP server · pipelines | OAuth 2.1 + MCP tools (Claude + ChatGPT verified); pipeline scheduling primitive | ✅ accepted |
| **M6** Chat-distiller | Stance-gated conversational ingestion, review queue, dedup sweep, one-tap remove | ✅ accepted |
| **M7 / M8 / M8.1** Map · ops · nav | Constellation explorer; ops console + observability; 6-tab UI consolidation | ✅ accepted |
| **M8.2** Data quality | Interiority (inner voice) + temporal correctness (`[[t:…]]` tokens, anchored resolution) | ✅ accepted |
| **M9** Multi-modal ingestion | Vision routing group, media substrate, PWA photo capture ([ADR-057](../second-brain-docs/adr/057-multimodal-media-ingestion-substrate.md)) | 🔜 build-ready |
| **M9.5** Instagram DMs | Export-first connector: triage tool, conversation substrate, sessionized stance-gated distillation ([ADR-058](../second-brain-docs/adr/058-instagram-dm-connector-and-conversation-substrate.md)) | 🔜 build-ready |
| **M10** Reflection agent | 1d/1w/1m/1y reflections + push notifications | ⏳ planned |
| **M11** Life-manager agent | Schedule / tasks / goals | ⏳ planned |
| **M12** Slack connector | Stance-gated Slack ingestion over the M9.5 substrate | ⏳ planned |

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
├── tools/      local one-shot tooling (M9.5: the Instagram export prep tool)
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
everything-visible, secrets-never-in-git, LLMs-classify-code-computes) live in
[CLAUDE.md](CLAUDE.md).

## License

Source-available under the **PolyForm Noncommercial License 1.0.0** ([LICENSE.md](LICENSE.md)):
free for any noncommercial purpose, attribution required (keep the `Required Notice:` line).
**Commercial use requires a separate paid license** — see [COMMERCIAL.md](COMMERCIAL.md).
