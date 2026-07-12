#!/usr/bin/env bash
# provision.sh — one-shot VPS bring-up (07-infrastructure.md). Target: DR < 30 min.
# Dormant until the provisioning session (ADR-012). Idempotent where practical.
#
# Prereqs done by hand once: DNS in Cloudflare (proxied), this repo cloned to /srv/app,
# deploy/.env filled from .env.example, GitHub deploy key present for the vault repo.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/app}"
VAULT_DIR="${VAULT_DIR:-/srv/vault}"
VAULT_REPO="${VAULT_REPO:-}"   # git@github.com:you/second-brain-vault.git

echo "==> System packages + Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

echo "==> Firewall (UFW: allow SSH + HTTPS only)"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

echo "==> Vault: restore from GitHub (source of truth lives in git, ADR-001)"
if [ -n "$VAULT_REPO" ] && [ ! -d "$VAULT_DIR/.git" ]; then
  git clone "$VAULT_REPO" "$VAULT_DIR"
else
  echo "    (skipping clone: VAULT_REPO unset or vault already present)"
fi

echo "==> Build + start the stack"
cd "$APP_DIR"
docker compose -f deploy/docker-compose.yml up -d --build

echo "==> Apply migrations explicitly (ADR-011: never in the request/boot path)"
docker compose -f deploy/docker-compose.yml exec -T api uv run alembic upgrade head

echo "==> Claude Max login (interactive, once; credentials persist on a volume)"
echo "    Run:  docker compose -f deploy/docker-compose.yml exec api claude login"

echo "==> Reindex the vault into Postgres (rebuild derived index)"
echo "    Run once the app is up:  curl -fsS -X POST https://\$BRAINDAN_DOMAIN/api/v1/admin/reindex"

echo "==> Done. Check health:  curl -fsS https://\$BRAINDAN_DOMAIN/api/v1/health"
