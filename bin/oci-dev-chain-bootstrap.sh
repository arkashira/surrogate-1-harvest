#!/usr/bin/env bash
# Dev-chain bootstrap — clones all 8 axentx project repos onto the OCI
# coordinator, sets up git identity for autonomous push, prepares the
# swarm-shared workspace, and starts the agent dev loop on schedule.
#
# Run once on the OCI coordinator (after oci-coordinator-bootstrap.sh):
#   curl -sSL https://raw.githubusercontent.com/arkashira/surrogate-1-harvest/main/bin/oci-dev-chain-bootstrap.sh | sudo bash
#
# Required env (already in /etc/surrogate-coordinator.env or pass inline):
#   GITHUB_TOKEN_ARKASHIRA  — push access for arkashira/* repos
#   GITHUB_TOKEN_AXENTX     — push access for AXENTX/* repos
#   ANTHROPIC_API_KEY       — for Claude-based agent calls
set -euo pipefail

DEV_USER="ubuntu"
[ -d "/home/opc" ] && [ ! -d "/home/ubuntu/.ssh" ] && DEV_USER="opc"
AXENTX_ROOT="/opt/axentx"
REPO_ROOT="/opt/surrogate-1-harvest"

echo "[dev-chain] target user: $DEV_USER"
echo "[dev-chain] axentx root: $AXENTX_ROOT"

# ── 1. apt deps for dev work
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q --no-install-recommends \
    git nodejs npm python3-pip jq make build-essential

# ── 2. clone all axentx project repos
mkdir -p "$AXENTX_ROOT"
chown -R "$DEV_USER:$DEV_USER" "$AXENTX_ROOT"

# Repo list — 8 projects per Mac inventory.
# Tokens are read from /etc/surrogate-coordinator.env (sourced by systemd).
ARK_TOK="${GITHUB_TOKEN_ARKASHIRA:-${GITHUB_TOKEN:-}}"
AX_TOK="${GITHUB_TOKEN_AXENTX:-$ARK_TOK}"

# GitHub PAT format requires 'x-access-token' as the URL user, not the
# org/user name. With the bare org name, GitHub returns:
#   "Invalid username or token. Password authentication is not supported"
declare -A REPOS=(
    [Costinel]="https://x-access-token:${ARK_TOK}@github.com/arkashira/Costinel.git"
    [Vanguard]="https://x-access-token:${ARK_TOK}@github.com/arkashira/vanguard.git"
    [arkship]="https://x-access-token:${ARK_TOK}@github.com/arkashira/arkship.git"
    [surrogate]="https://x-access-token:${ARK_TOK}@github.com/arkashira/surrogate.git"
    [workio]="https://x-access-token:${ARK_TOK}@github.com/arkashira/workio.git"
    [surrogate-1]="https://x-access-token:${AX_TOK}@github.com/AXENTX/surrogate-1.git"
)

for proj in "${!REPOS[@]}"; do
    target="$AXENTX_ROOT/$proj"
    if [ -d "$target/.git" ]; then
        echo "[dev-chain] $proj exists — pull"
        sudo -u "$DEV_USER" git -C "$target" pull --ff-only 2>&1 | tail -2 || true
    else
        echo "[dev-chain] cloning $proj …"
        sudo -u "$DEV_USER" git clone --depth 1 "${REPOS[$proj]}" "$target" 2>&1 | tail -2 || \
            echo "[dev-chain] ⚠ $proj clone failed (token? rename?)"
    fi
done

# ── 3. git identity for autonomous commits (per-repo, scoped to axentx work)
sudo -u "$DEV_USER" git config --global user.email "axentx-dev-bot@axentx.local"
sudo -u "$DEV_USER" git config --global user.name "axentx-dev-bot"
sudo -u "$DEV_USER" git config --global push.default simple
sudo -u "$DEV_USER" git config --global pull.rebase false
sudo -u "$DEV_USER" git config --global --add safe.directory "*"

# ── 4. swarm-shared workspace skeleton
SWARM="$REPO_ROOT/state/swarm-shared"
mkdir -p "$SWARM"/{decisions,backlog,past-cycles}
chown -R "$DEV_USER:$DEV_USER" "$SWARM"

# ── 5. systemd service for sprint ceremonies (twice daily — 9am + 5pm Bangkok)
cat > /etc/systemd/system/axentx-sprint-ceremony.service <<EOF
[Unit]
Description=axentx mini-sprint planning + retro
After=surrogate-coordinator.service

[Service]
Type=oneshot
User=$DEV_USER
WorkingDirectory=$REPO_ROOT
EnvironmentFile=/etc/surrogate-coordinator.env
ExecStart=/bin/bash -c '$REPO_ROOT/bin/claude-mini-sprint.sh && sleep 30 && $REPO_ROOT/bin/claude-mini-retro.sh'
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/axentx-sprint-ceremony.timer <<EOF
[Unit]
Description=Run axentx ceremonies twice daily (02:00 + 10:00 UTC = 09:00 + 17:00 Bangkok)

[Timer]
OnCalendar=*-*-* 02:00:00
OnCalendar=*-*-* 10:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now axentx-sprint-ceremony.timer

echo "[dev-chain] ✓ done"
echo "  axentx repos:        $AXENTX_ROOT/{$(ls $AXENTX_ROOT 2>/dev/null | tr '\n' ',' | sed 's/,$//')}"
echo "  ceremony timer:      $(systemctl is-enabled axentx-sprint-ceremony.timer 2>&1)"
echo "  dev-loop driver:     hermes-cron-dispatcher (already running, jobs.json @ */15)"
echo ""
echo "[dev-chain] verify: systemctl list-timers | grep axentx"
