# Braindan — your personal second brain

**A conversational second brain, available anywhere, any time.** Talk or type a thought and it's
captured in seconds; the AI organizes it across the planes of your life, links it to what it relates
to, and lets you search, question and understand your own history. The goal isn't storage — it's an
extension of memory and a reflection partner. Information → knowledge → understanding, compounding
over time.

Your memory lives as **plain Markdown you own** — a git-versioned vault on your own server, fully
recoverable, and openable in Obsidian whenever you want to wander through it by hand. Everything else
(search index, relatedness graph, chat) is derived and rebuildable from those files.

> **Single-user, self-hosted, private by design.** It runs on your own always-on VPS behind
> Cloudflare — no personal machine required, no third party holding your thoughts.

---

## What it does for you

- **🎙️ Frictionless capture.** One tap from your phone's lock screen — speak or type — and you're
  done in under 10 seconds. No titles, no tags, no "which folder?". Voice is transcribed
  automatically (Groq → OpenAI Whisper fallback).
- **🧠 The AI files it, not you.** Each capture is split into **atomic notes**, classified into the
  **planes of your life** (`Professional · Personal · Family · Friends · Health · Ideas`, all
  configurable), tagged by theme, and cross-linked. A capture it can't place lands in `Inbox/` —
  never guessed.
- **🔎 Semantic search over everything.** Ask in your own words and find the right note by meaning,
  not keywords — self-hosted `nomic` embeddings + pgvector, with plane filters and a read-only
  preview.
- **🕸️ A relatedness graph that reflects *meaning*.** Notes are automatically linked to the ones
  they're topically about (distinct from mere co-capture), rendered as an Obsidian-visible
  `## Related notes` block so your vault's graph view actually maps how your thinking connects.
- **💬 Chat over your whole memory.** *(roadmap — M3)* Ask questions and get answers grounded in your
  notes with `[n]` source citations, on the model you pick, with an honest "not in your notes" when
  it isn't.
- **🤖 Agents that feed the brain.** *(roadmap — M4)* Scheduled connectors (Slack first) pull in what
  you said and discussed elsewhere, distill it, and file it into the right plane overnight.
- **✨ Background intelligence.** *(roadmap — M5)* Daily and weekly reviews surface themes, decisions,
  patterns and open questions across planes — insight, not just summaries.
- **🔐 Yours, and unloseable.** Raw captures are persisted before any model call and never dropped;
  the vault is git-versioned with fast-forward-only push, off-site WORM backups to R2, and a weekly
  integrity drill. Any component can burn down without memory loss.
- **🔁 Model independence with a preference.** Claude (Max subscription, via the Agent SDK) is the
  primary mind; automatic fallback to Nebius; embeddings self-hosted. Every model call goes through
  one provider registry, and every fallback is recorded, never silent.

## How it works

```
   Phone / Desktop  ──HTTPS──►  Hetzner VPS (always on, behind Cloudflare)
   PWA: capture,                ├─ Caddy (TLS, serves the PWA, proxies /api)
   search, chat,                ├─ FastAPI service — capture / indexing / search / chat pipelines
   activity feed                ├─ Provider registry — Claude → Nebius (chat), Groq → OpenAI (STT),
                                │    self-hosted nomic via an ollama sidecar (embeddings)
                                ├─ Scheduler — ingestion + analysis in a nightly 03:00–05:00 window
                                │
   Obsidian  ◄─obsidian-git─┐   ├─ Vault (Markdown, THE source of truth) ──git push──► private GitHub
   (optional)              └───►└─ Supabase Postgres + pgvector (derived index + operational state)
```

**Vault is truth; the database is a cache.** Content only ever flows vault → index, never back.
Drop every derived table and one `POST /admin/reindex` rebuilds search and the relatedness graph
from the Markdown. The web client and server share nothing but the HTTP contract.

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
| **M1** Capture | Voice/text → organized atomic notes; full vault durability | ✅ code-complete (backup tail folds into M2) |
| **M2** Indexing & search | Embeddings, indexer, semantic `/search`, relatedness graph | 🔨 in progress (Tasks 1–5 done) |
| **M3** Chat | Retrieval + citations + sessions + model picker | ⏳ planned |
| **M4** Agents | Slack connector + distiller + activity feed | ⏳ planned |
| **M5** Background intelligence | Daily/weekly reviews, PWA polish | ⏳ planned |

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
(vault-is-truth, provider boundary, plain SQL, idempotency, everything-visible, secrets-never-in-git)
live in [CLAUDE.md](CLAUDE.md).

## License

Source-available under the **PolyForm Noncommercial License 1.0.0** ([LICENSE.md](LICENSE.md)):
free for any noncommercial purpose, attribution required (keep the `Required Notice:` line).
**Commercial use requires a separate paid license** — see [COMMERCIAL.md](COMMERCIAL.md).
