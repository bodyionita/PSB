# Braindan server

FastAPI service. Plain SQL over asyncpg (no ORM); Alembic migrations authored as explicit
SQL (ADR-011). Only `app/providers/` may import vendor SDKs (ADR-004).

## Layout

```
app/
├── main.py            FastAPI factory + lifespan (builds app.state singletons)
├── config.py          pydantic-settings — the ONLY place env is read (rule 9)
├── db.py              the one module that owns the asyncpg pool
├── security.py        Argon2id + session-token hashing (pure)
├── dependencies.py    FastAPI DI: get_db / require_session / …
├── migration_check.py startup "are we behind head?" warning (no SQLAlchemy import)
├── routers/           health, auth  (validation + delegation only)
├── services/          auth_service, rate_limit, system_health (business logic)
└── providers/         registry + fallback chain; openai-compatible + claude-max
migrations/            Alembic env + versions/001_initial_schema.py (full schema)
scripts/hash_password.py
tests/                 unit tests — fakes for providers, no live LLMs/DB
```

## Dev

```bash
uv sync                                        # or: python -m venv .venv && pip install -e ".[dev]"
cp .env.example .env
python scripts/hash_password.py 'dev-pass'     # paste into API_PASSWORD_HASH
uv run alembic upgrade head                    # against docker-compose.dev.yml DB
uv run uvicorn app.main:app --reload
uv run pytest
uv run ruff check .
```

Endpoints are served under `/api/v1` (Caddy proxies `/api` → FastAPI). M0 exposes
`/health`, `/auth/login`, `/auth/logout`, `/auth/me`; later milestones add capture, chat,
search, activity, settings, agents, admin (see `../../second-brain-docs/03-api.md`).

## Notes

- **Migrations are never applied in the request/boot path** (ADR-011). Run
  `alembic upgrade head` explicitly (CI / provision.sh); the service only warns if behind.
- **claude-max is health-guarded** (ADR-012): with no local Claude CLI/login, the chat
  chain falls back to Nebius and records `fallback_used`. `claude login` on the VPS lights
  up the real path with zero code change.
- `/health` never calls an LLM, so the service boots with no API keys.
