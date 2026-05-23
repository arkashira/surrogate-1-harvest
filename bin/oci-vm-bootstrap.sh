#!/usr/bin/env bash
# oci-vm-bootstrap.sh — bring a fresh OCI VM up to parity with the GCP fleet.
#
# Pulls the surrogate-1-harvest repo, the `state` orphan branch (with all
# decisions/skills/papers/profiles), installs the systemd units, starts the
# daemon fleet. After this completes, the new VM is a peer of GCP — both
# heartbeat to the same /dash/agents, both write to the same state branch,
# both run the same pipeline.
#
# Idempotent: safe to re-run. Skips steps that already completed.
set -euo pipefail

LOG_PREFIX="[oci-bootstrap]"
log() { echo "$LOG_PREFIX $(date -u +%H:%M:%SZ) $*"; }

REPO_URL="${REPO_URL:-https://github.com/arkashira/surrogate-1-harvest.git}"
REPO_DIR="${REPO_DIR:-/opt/surrogate-1-harvest}"
STATE_DIR="${STATE_DIR:-/opt/surrogate-1-state}"
ENV_FILE="${ENV_FILE:-/etc/surrogate-coordinator.env}"

# ── 0. Detect arch / OS ──────────────────────────────────────────────────
ARCH="$(uname -m)"
log "arch=$ARCH host=$(hostname)"

# ── 1. System packages ───────────────────────────────────────────────────
log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv git curl jq rsync postgresql-client \
  ca-certificates >/dev/null

# Node 22 binary tarball — same trick as GCP since apt repos lag.
if ! command -v node22 >/dev/null; then
  log "installing Node 22 (binary tarball)"
  case "$ARCH" in
    aarch64) NODE_ARCH=arm64 ;;
    x86_64)  NODE_ARCH=x64 ;;
    *) log "unknown arch $ARCH"; exit 1 ;;
  esac
  cd /tmp
  curl -fsSL "https://nodejs.org/dist/v22.10.0/node-v22.10.0-linux-${NODE_ARCH}.tar.xz" \
    -o node22.tar.xz
  tar xf node22.tar.xz -C /opt/
  ln -sf "/opt/node-v22.10.0-linux-${NODE_ARCH}/bin/node" /usr/local/bin/node22
  ln -sf "/opt/node-v22.10.0-linux-${NODE_ARCH}/bin/npm"  /usr/local/bin/npm22
  ln -sf "/opt/node-v22.10.0-linux-${NODE_ARCH}/bin/npx"  /usr/local/bin/npx22
fi

# ── 2. Clone harvest repo + state branch ─────────────────────────────────
if [ ! -d "$REPO_DIR/.git" ]; then
  log "cloning $REPO_URL → $REPO_DIR"
  git clone --depth=20 "$REPO_URL" "$REPO_DIR"
fi
chown -R ubuntu:ubuntu "$REPO_DIR"

if [ ! -d "$STATE_DIR/.git" ]; then
  log "cloning state branch → $STATE_DIR"
  mkdir -p "$STATE_DIR"
  chown ubuntu:ubuntu "$STATE_DIR"
  sudo -u ubuntu git clone --depth=10 --branch state "$REPO_URL" "$STATE_DIR" || \
    log "  ⚠ state branch clone failed — state-sync will retry"
fi

# ── 3. Python venv + deps ────────────────────────────────────────────────
if [ ! -d "$REPO_DIR/.venv" ]; then
  log "creating venv"
  sudo -u ubuntu python3 -m venv "$REPO_DIR/.venv"
fi
sudo -u ubuntu bash -c "source $REPO_DIR/.venv/bin/activate && \
  pip install --quiet --upgrade pip && \
  pip install --quiet discord.py requests"

# ── 4. /etc/surrogate-coordinator.env ────────────────────────────────────
# Operator must seed this file with API keys BEFORE running this script,
# OR mount it from a shared secret store. We bail with an instructive
# message if it's missing.
if [ ! -f "$ENV_FILE" ]; then
  cat <<EOF
$LOG_PREFIX MISSING $ENV_FILE
$LOG_PREFIX seed it with the same shape as on the GCP host. Required keys:
$LOG_PREFIX   GROQ_API_KEY  CHUTES_API_KEY  SAMBANOVA_API_KEY  NVIDIA_NIM_API_KEY
$LOG_PREFIX   GOOGLE_API_KEY  CLOUDFLARE_API_TOKEN  CLOUDFLARE_ACCOUNT_ID
$LOG_PREFIX   HEARTBEAT_KV_ID  GITHUB_TOKEN  GITHUB_TOKEN_ARKASHIRA  HF_TOKEN
$LOG_PREFIX   SUPABASE_URL  SUPABASE_SECRET_KEY  DISCORD_WEBHOOK
$LOG_PREFIX   USE_V1_FALLBACK=0
$LOG_PREFIX
$LOG_PREFIX after seeding, re-run this script (it's idempotent).
EOF
  exit 3
fi
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

# ── 5. Install systemd units (from the harvest repo) ─────────────────────
log "installing systemd units"
cp "$REPO_DIR"/systemd/*.service /etc/systemd/system/
cp "$REPO_DIR"/systemd/*.timer 2>/dev/null /etc/systemd/system/ || true
systemctl daemon-reload

# ── 6. Enable + start the canonical fleet ────────────────────────────────
SERVICES_ALWAYS_ON=(
  surrogate-state-sync-daemon
  surrogate-self-heal-daemon
  surrogate-watchdog
  surrogate-discord-bot
  axentx-incident-responder-daemon
  axentx-scheduled-runner-daemon
  axentx-skill-synthesizer-daemon
  axentx-pain-validator-daemon
  axentx-research-daemon@1
  axentx-research-daemon@2
  axentx-research-daemon@3
  axentx-bd-daemon
  axentx-design-thinking-daemon
  axentx-business-daemon
  axentx-marketing-daemon
  axentx-prd-daemon
  axentx-pm-daemon
  axentx-architect-daemon
  axentx-perf-daemon
  axentx-security-daemon
  axentx-docs-daemon
  axentx-release-daemon
  axentx-ux-daemon
  axentx-content-daemon
  axentx-trends-daemon
  axentx-customer-poll-daemon
  axentx-canary-daemon
  axentx-support-inbox-daemon
  axentx-reviewer-daemon
  axentx-qa-daemon
  axentx-commit-daemon
  axentx-dev-daemon@1
  axentx-dev-daemon@2
  axentx-dev-daemon@3
  axentx-dev-daemon@4
  axentx-dev-daemon@5
  axentx-dev-daemon@6
)

for svc in "${SERVICES_ALWAYS_ON[@]}"; do
  systemctl enable --now "$svc" 2>&1 | grep -v "^Created symlink" || true
done

log "✓ bootstrap complete — host should appear on /dash/agents within 60s"
log "  (each daemon's heartbeat tags 'host=$(hostname)')"
