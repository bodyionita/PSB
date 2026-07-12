#!/usr/bin/env bash
# provision.sh — one-shot VPS box prep (07-infrastructure.md). Target: DR < 30 min.
# Dormant until the provisioning session (ADR-012). Idempotent where practical.
#
# CI is the SOLE writer of deploy/.env and starts the app (ADR-016). This script only
# PREPS THE BOX. When it finishes, trigger the deploy workflow (push to main, or
# GitHub -> Actions -> Run workflow) to render deploy/.env from the GitHub `production`
# environment secrets, build, and start the stack.
#
# Prereqs done by hand once: DNS in Cloudflare (proxied); this repo cloned to /srv/app;
# the GitHub `production` environment secrets set (VPS_HOST/VPS_USER/VPS_SSH_KEY + the app
# secrets in ADR-016). Non-secret config is versioned in deploy/defaults.env.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/app}"
VAULT_DIR="${VAULT_DIR:-/srv/vault}"
VAULT_REPO="${VAULT_REPO:-git@github.com:bodyionita/PSB-vault.git}"
VAULT_DEPLOY_KEY="${VAULT_DEPLOY_KEY:-/root/.ssh/vault_deploy_key}"

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

echo "==> Vault deploy key (generated on the box; private half never leaves — ADR-016)"
if [ ! -f "$VAULT_DEPLOY_KEY" ]; then
  install -d -m 700 "$(dirname "$VAULT_DEPLOY_KEY")"
  ssh-keygen -t ed25519 -N "" -C "braindan-vault-deploy" -f "$VAULT_DEPLOY_KEY"
  echo "    Add this PUBLIC key to $VAULT_REPO"
  echo "    -> Settings -> Deploy keys -> Add key -> [x] Allow write access:"
  echo "    ----8<----"
  cat "${VAULT_DEPLOY_KEY}.pub"
  echo "    ---->8----"
  echo "    Then re-run this script to clone the vault."
fi

echo "==> Vault: restore from GitHub (source of truth lives in git, ADR-001)"
if [ ! -d "$VAULT_DIR/.git" ]; then
  if GIT_SSH_COMMAND="ssh -i $VAULT_DEPLOY_KEY -o IdentitiesOnly=yes" \
       git clone "$VAULT_REPO" "$VAULT_DIR"; then
    echo "    Vault cloned to $VAULT_DIR"
  else
    echo "    (clone failed — add the deploy key above to $VAULT_REPO with write access, then re-run)"
  fi
else
  echo "    (vault already present)"
fi

echo
echo "==> Box prepped. deploy/.env is written by CI, NOT here (ADR-016)."
echo "    Start (or update) the app by triggering the deploy workflow:"
echo "      - push to main, or GitHub -> Actions -> ci -> Run workflow"
echo "    It renders deploy/.env, then 'docker compose up -d --build' + 'alembic upgrade head'."
echo
echo "==> After the stack is up, once:"
echo "    Claude Max OAuth:  docker compose -f $APP_DIR/deploy/docker-compose.yml exec api claude login"
echo "    Health check:      curl -fsS https://${BRAINDAN_DOMAIN:-braindan.cc}/api/v1/health"
