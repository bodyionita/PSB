# Braindan API image (ADR-003: single service = FastAPI + scheduler + agents).
# Includes git (vault backup) and the Claude Code CLI (provider claude-max, ADR-004):
# OAuth is done once on the VPS via `docker compose exec api claude login`, credentials
# persisted on a volume (07-infrastructure.md).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:/usr/local/share/npm-global/bin:$PATH"

# System deps: git + openssh-client for vault backup (SSH push to GitHub), nodejs/npm for the
# Claude Code CLI, curl for uv.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# /srv/vault is bind-mounted and owned by the host 'deploy' user, but this container runs
# as root — git refuses ("dubious ownership"). Mark it safe so the /health git-remote probe
# and the M1 vault auto-backup (commit/push) work.
RUN git config --global --add safe.directory /srv/vault

# Entrypoint wires the bind-mounted vault deploy key into a root-owned SSH setup so the vault
# push authenticates to GitHub (ADR-014). No-op if the key isn't mounted.
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# uv for env + lockfile + running (ADR-012), same tool as local dev.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

# Claude Code CLI (the claude-max provider shells this; OAuth via a mounted volume).
RUN npm config set prefix /usr/local/share/npm-global \
    && npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Install dependencies first for layer caching.
COPY server/pyproject.toml server/uv.lock* ./
RUN uv sync --no-dev --frozen || uv sync --no-dev

# App source + migrations.
COPY server/ ./

EXPOSE 8000

# Migrations are applied explicitly by provision.sh / CI (ADR-011), never at boot.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
