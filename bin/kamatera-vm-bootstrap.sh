#!/usr/bin/env bash
# kamatera-vm-bootstrap.sh — bring a fresh Kamatera VM up to fleet parity.
#
# Same idea as oci-vm-bootstrap.sh but tuned for Kamatera VM specs (4-8 CPU,
# 16-32 GB RAM, 100 GB SSD — much beefier than GCP e2-micro). We can run
# the FULL daemon fleet plus heavy bypass workloads (Crawl4AI / Playwright
# stealth) here without RSS pressure.
#
# Idempotent: safe to re-run.
set -euo pipefail

LOG_PREFIX="[kamatera-bootstrap]"
log() { echo "$LOG_PREFIX $(date -u +%H:%M:%SZ) $*"; }

REPO_URL="${REPO_URL:-https://github.com/arkashira/surrogate-1-harvest.git}"
REPO_DIR="${REPO_DIR:-/opt/surrogate-1-harvest}"
STATE_DIR="${STATE_DIR:-/opt/surrogate-1-state}"
ENV_FILE="${ENV_FILE:-/etc/surrogate-coordinator.env}"

# ── 1. System packages ───────────────────────────────────────────────────
log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv git curl jq rsync \
  build-essential ca-certificates xz-utils \
  >/dev/null

# Node 22 binary tarball (apt repo lags)
if ! command -v node22 >/dev/null; then
  log "installing Node 22 (binary tarball)"
  ARCH="$(uname -m)"
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

# Optional heavy stack — Crawl4AI deps (Chromium for headless bypass)
if [ "${INSTALL_CRAWL4AI:-1}" = "1" ]; then
  log "installing Crawl4AI deps (Chromium ~200 MB)"
  apt-get install -y --no-install-recommends \
    chromium-browser chromium-driver \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    >/dev/null 2>&1 || log "  (chromium install skipped — package mismatch)"
fi

# ── 2. Clone harvest + state branch ──────────────────────────────────────
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

# Crawl4AI optional install
if [ "${INSTALL_CRAWL4AI:-1}" = "1" ]; then
  sudo -u ubuntu bash -c "source $REPO_DIR/.venv/bin/activate && \
    pip install --quiet 'crawl4ai>=0.6.0' || true"
fi

# ── 4. /etc/surrogate-coordinator.env ────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  cat <<EOF
$LOG_PREFIX MISSING $ENV_FILE
$LOG_PREFIX seed it from the GCP host: copy the same file via scp from
$LOG_PREFIX   gcloud compute scp surrogate-watchdog:/etc/surrogate-coordinator.env  /etc/
$LOG_PREFIX then chmod 600 + chown root:root and re-run this script.
EOF
  exit 3
fi
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

# ── 5. Install systemd units ─────────────────────────────────────────────
log "installing systemd units"
cp "$REPO_DIR"/systemd/*.service /etc/systemd/system/
cp "$REPO_DIR"/systemd/*.timer 2>/dev/null /etc/systemd/system/ || true
systemctl daemon-reload

# ── 6. Enable + start the canonical fleet ────────────────────────────────
SERVICES=(
  surrogate-state-sync-daemon
  surrogate-self-heal-daemon
  surrogate-watchdog
  axentx-incident-responder-daemon
  axentx-scheduled-runner-daemon
  axentx-skill-synthesizer-daemon
  axentx-pain-validator-daemon
  axentx-research-daemon@1 axentx-research-daemon@2 axentx-research-daemon@3
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
  axentx-dev-daemon@1 axentx-dev-daemon@2 axentx-dev-daemon@3
  axentx-dev-daemon@4 axentx-dev-daemon@5 axentx-dev-daemon@6
)

for svc in "${SERVICES[@]}"; do
  systemctl enable --now "$svc" 2>&1 | grep -v "^Created symlink" || true
done

log "✓ bootstrap complete — host $(hostname) joins /dash/agents within 60s"
