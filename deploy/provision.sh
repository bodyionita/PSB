#!/usr/bin/env bash
# provision.sh — one-shot VPS box prep (07-infrastructure.md). Target: DR < 30 min.
# Run as ROOT on a fresh VPS. Idempotent where practical.
#
# What it does (ADR-018): creates the non-root `deploy` user (docker + sudo groups, owns
# /srv/*), seeds its authorized_keys, installs Docker + UFW, generates the vault deploy key,
# restores the vault, and — as its FINAL, guarded step — hardens SSH (disables root login +
# password auth, keys-only). After it finishes, deploys come in as `deploy`.
#
# CI is the SOLE writer of deploy/.env AND the origin TLS files (ADR-016/017) and starts the
# app. This script only PREPS THE BOX. When it finishes, trigger the deploy workflow (push to
# main, or GitHub -> Actions -> Run workflow) to render deploy/.env + deploy/origin.{crt,key}
# from the GitHub `production` secrets, build, and start the stack.
#
# Prereqs done by hand once, as root:
#   - DNS in Cloudflare (proxied); this repo cloned to /srv/app.
#   - GitHub `production` secrets set (VPS_HOST, VPS_USER=deploy, VPS_SSH_KEY + the app
#     secrets in ADR-016 + ORIGIN_CERT_PEM/ORIGIN_KEY_PEM in ADR-017).
#   - The CI deploy PUBLIC key available to this script via CI_DEPLOY_PUBKEY (non-secret;
#     the public half of VPS_SSH_KEY). If unset, only the operator key (copied from root's
#     authorized_keys) is installed — set the deploy pubkey before CI can deploy.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "provision.sh must run as root on a fresh VPS." >&2
  exit 1
fi

DEPLOY_USER="${DEPLOY_USER:-deploy}"
DEPLOY_HOME="${DEPLOY_HOME:-/home/$DEPLOY_USER}"
APP_DIR="${APP_DIR:-/srv/app}"
VAULT_DIR="${VAULT_DIR:-/srv/vault}"
DATA_DIR="${DATA_DIR:-/srv/data}"
VAULT_REPO="${VAULT_REPO:-git@github.com:bodyionita/PSB-vault.git}"
VAULT_DEPLOY_KEY="${VAULT_DEPLOY_KEY:-$DEPLOY_HOME/.ssh/vault_deploy_key}"
CI_DEPLOY_PUBKEY="${CI_DEPLOY_PUBKEY:-}"   # non-secret public key; optional

echo "==> System packages + Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

echo "==> Non-root deploy user '$DEPLOY_USER' (docker + sudo groups) — ADR-018"
if ! id -u "$DEPLOY_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$DEPLOY_USER"
  echo "    Created $DEPLOY_USER. Set its password for sudo/console later: passwd $DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"
usermod -aG sudo "$DEPLOY_USER"

echo "==> Own /srv/* as $DEPLOY_USER"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR" "$VAULT_DIR" "$DATA_DIR"
# /srv/app may already hold the hand-cloned repo (root-owned) — hand it to deploy.
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR" "$VAULT_DIR" "$DATA_DIR"

echo "==> Seed $DEPLOY_USER authorized_keys (operator key from root + CI deploy pubkey)"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_HOME/.ssh"
DEPLOY_AK="$DEPLOY_HOME/.ssh/authorized_keys"
touch "$DEPLOY_AK"
# Operator key(s): Hetzner installs the key chosen at creation into /root/.ssh/authorized_keys.
# Key-type token, optionally after options and/or the FIDO2 "sk-" prefix (used by the guard too).
KEYRE='(^|[[:space:]])(sk-)?(ssh-(ed25519|rsa)|ecdsa-sha2-)'
OPERATOR_KEY_SEEDED=0
if [ -f /root/.ssh/authorized_keys ] && grep -qE "$KEYRE" /root/.ssh/authorized_keys; then
  cat /root/.ssh/authorized_keys >> "$DEPLOY_AK"
  OPERATOR_KEY_SEEDED=1
fi
# CI deploy public key (non-secret), passed in via env.
if [ -n "$CI_DEPLOY_PUBKEY" ]; then
  printf '%s\n' "$CI_DEPLOY_PUBKEY" >> "$DEPLOY_AK"
fi
# Dedupe + lock down ownership/perms.
sort -u "$DEPLOY_AK" -o "$DEPLOY_AK"
chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_AK"
chmod 600 "$DEPLOY_AK"

echo "==> Firewall (UFW: allow SSH + HTTPS only — no :80, origin is 443-only per ADR-017)"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

echo "==> Vault deploy key (generated on the box as $DEPLOY_USER; private half never leaves — ADR-016)"
if [ ! -f "$VAULT_DEPLOY_KEY" ]; then
  runuser -u "$DEPLOY_USER" -- ssh-keygen -t ed25519 -N "" -C "braindan-vault-deploy" -f "$VAULT_DEPLOY_KEY"
  echo "    Add this PUBLIC key to $VAULT_REPO"
  echo "    -> Settings -> Deploy keys -> Add key -> [x] Allow write access:"
  echo "    ----8<----"
  cat "${VAULT_DEPLOY_KEY}.pub"
  echo "    ---->8----"
  echo "    Then re-run this script AS ROOT to clone the vault and finish (incl. SSH hardening)."
fi

echo "==> Vault: restore from GitHub (source of truth lives in git, ADR-001)"
# VAULT_READY gates SSH hardening below: on a fresh box the vault deploy key is generated
# here (private half never leaves), so the FIRST run cannot clone yet — the pubkey isn't on
# GitHub. We therefore DON'T harden (disable root) until the box is actually complete, so both
# passes run as root. Pass 1: prints the vault pubkey, clone fails, hardening deferred. Add the
# key to the vault repo (write), then re-run AS ROOT: pass 2 clones and hardens (final step).
VAULT_READY=0
if [ -d "$VAULT_DIR/.git" ]; then
  echo "    (vault already present)"
  VAULT_READY=1
elif runuser -u "$DEPLOY_USER" -- \
       env GIT_SSH_COMMAND="ssh -i $VAULT_DEPLOY_KEY -o IdentitiesOnly=yes" \
       git clone "$VAULT_REPO" "$VAULT_DIR"; then
  echo "    Vault cloned to $VAULT_DIR"
  VAULT_READY=1
else
  echo "    (clone failed — add the deploy key above to $VAULT_REPO with WRITE access, then re-run)"
fi

echo "==> Harden SSH (FINAL step, guarded — ADR-018): keys-only, no root login"
# Defer until the box is fully provisioned (vault cloned) so root stays reachable across the
# two-pass vault-key bootstrap; re-running as root after adding the key completes + hardens.
if [ "$VAULT_READY" -ne 1 ]; then
  echo "    DEFERRED: vault not cloned yet, so the box isn't finished. Root login stays ENABLED." >&2
  echo "    Add the vault deploy key printed above to $VAULT_REPO (write access), then re-run" >&2
  echo "    this script AS ROOT to clone the vault and harden SSH. (Nothing else to redo.)" >&2
  exit 0
fi
# Guard (fail-safe): never disable root login + password auth unless deploy has a usable key,
# or this box locks out. Matches modern types incl. FIDO2 (sk-) and options-prefixed lines.
if ! grep -qE "$KEYRE" "$DEPLOY_AK"; then
  echo "    REFUSING to harden: $DEPLOY_AK has no valid public key." >&2
  echo "    Seed the operator key (root's authorized_keys) and/or CI_DEPLOY_PUBKEY, then re-run." >&2
  exit 1
fi
# The guard proves *a* key exists; warn if the operator's own key isn't among them, since
# PasswordAuthentication no would then leave only the provider console for interactive access.
if [ "$OPERATOR_KEY_SEEDED" -ne 1 ]; then
  echo "    WARNING: no operator key found in /root/.ssh/authorized_keys — only the CI key (if any)" >&2
  echo "    is installed for '$DEPLOY_USER'. Add your personal key to $DEPLOY_AK before you rely on" >&2
  echo "    SSH; until then your only interactive access is the Hetzner web console." >&2
fi
HARDEN_CONF=/etc/ssh/sshd_config.d/10-braindan-hardening.conf
cat > "$HARDEN_CONF" <<'EOF'
# Braindan SSH hardening (ADR-018). Keys-only; no direct root login. Root remains
# reachable via `sudo` as the deploy user, plus the provider console as a rescue hatch.
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
EOF
sshd -t                                    # validate before reloading (fail closed)
# Tolerate a reload-command miss: sshd -t already validated, and the current session is
# untouched, so don't abort the run — the validated drop-in applies on the next sshd start.
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || service ssh reload 2>/dev/null \
  || echo "    WARN: could not reload sshd now; the validated config applies on its next restart." >&2
echo
echo "    !!  SSH HARDENED. BEFORE YOU LOG OUT: open a SECOND terminal and confirm"
echo "    !!    ssh $DEPLOY_USER@<host>"
echo "    !!  works (key auth). Root SSH is now disabled; use 'sudo' as $DEPLOY_USER."
echo "    !!  If locked out, recover via the Hetzner web console."

echo
echo "==> Box prepped. deploy/.env + origin.{crt,key} are written by CI, NOT here (ADR-016/017)."
echo "    Before CI can deploy: set VPS_USER=$DEPLOY_USER and ensure the CI deploy pubkey is in"
echo "    $DEPLOY_AK (via CI_DEPLOY_PUBKEY above). Also set the deploy password: passwd $DEPLOY_USER"
echo "    Start (or update) the app by triggering the deploy workflow:"
echo "      - push to main, or GitHub -> Actions -> ci -> Run workflow"
echo "    It renders deploy/.env + origin TLS files, then 'docker compose up -d --build' + 'alembic upgrade head'."
echo
echo "==> After the stack is up, once (run as $DEPLOY_USER — it's in the docker group):"
echo "    Claude Max OAuth:  docker compose -f $APP_DIR/deploy/docker-compose.yml exec api claude login"
echo "    Pull embeddings:   docker compose -f $APP_DIR/deploy/docker-compose.yml exec ollama ollama pull nomic-embed-text"
echo "    Health check:      curl -fsS https://${BRAINDAN_DOMAIN:-braindan.cc}/api/v1/health"
