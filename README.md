# Braindan — Personal Second Brain (code monorepo)

Implementation of the system designed in [`../second-brain-docs/`](../second-brain-docs/).
**The docs repo is the contract.** Read it first (`README → 00…09 → adr/`). This repo only
implements it; do not diverge from an ADR without writing a new one in the docs repo.

## Layout ([ADR-006](../second-brain-docs/adr/006-monorepo-with-strict-server-web-decoupling.md))

```
second-brain/
├── server/     FastAPI service (Python 3.12, uv). Plain SQL over asyncpg, no ORM.
├── web/        React + Vite + TS PWA (pnpm). Talks only to the HTTP API.
├── deploy/     Dockerfiles, compose, Caddy, provision.sh — written now, dormant until provisioning.
└── .github/    CI (per-directory path filters).
```

`server/` and `web/` share **nothing** but the HTTP contract in
[`03-api.md`](../second-brain-docs/03-api.md). Each builds and runs standalone from its own
directory.

## Status — M0 (Foundations), local-first build

Per [ADR-012](../second-brain-docs/adr/012-m0-implementation-stack.md), M0 is split into:

- **(a) local-first build (this repo, now):** complete `server/` + `web/` + `deploy/`,
  verified to boot end-to-end on the dev machine (dockerized `pgvector` dev DB →
  `alembic upgrade head` → `/health` green → login/session → registry fallback unit tests →
  `pnpm build` + web shell clicks through).
- **(b) provisioning session (later):** live Hetzner VPS + Cloudflare + Supabase + GitHub
  remotes + `claude login`. Deploy artifacts here are written but dormant until then.

## Quick start (local dev)

### Database
```bash
docker compose -f deploy/docker-compose.dev.yml up -d      # pgvector on localhost:5432
```

### Server
```bash
cd server
uv sync                                                    # or: python -m venv .venv && pip install -e ".[dev]"
cp .env.example .env                                       # then set API_PASSWORD_HASH (see below)
uv run python scripts/hash_password.py 'your-dev-password' # prints the argon2 hash for .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload                       # http://localhost:8000/api/v1/health
uv run pytest                                              # unit tests (no live LLMs/DB required)
```

### Web  (Node 24 + pnpm 9; run `corepack enable` once)
```bash
cd web
pnpm install
pnpm dev                                                   # http://localhost:5173
pnpm build                                                 # type-check (tsc) + production build
```

See each subdirectory's `README.md` for details.

## License

Source-available under the **PolyForm Noncommercial License 1.0.0** ([LICENSE.md](LICENSE.md)):
free for any noncommercial purpose, attribution required (keep the `Required Notice:` line).
**Commercial use requires a separate paid license** — see [COMMERCIAL.md](COMMERCIAL.md).
