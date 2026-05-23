#!/usr/bin/env bash
# Install 5 axentx pipeline daemons as systemd services.
# Run once on the OCI coordinator after oci-dev-chain-bootstrap.sh.
#
# Daemons (each = one Python process, lightweight, polls work queue):
#   axentx-dev-daemon      → produces (project, focus) tasks every 5 min
#   axentx-reviewer-daemon → consumes review-queue
#   axentx-qa-daemon       → consumes qa-queue
#   axentx-commit-daemon   → consumes commit-queue, pushes to GitHub
#   axentx-pm-daemon       → mini-sprint/retro every 4h
#
# All run under user 'ubuntu', read /etc/surrogate-coordinator.env for
# API keys + tokens, log to journal + /opt/.../logs/axentx-*-daemon.log
set -euo pipefail

REPO_ROOT="/opt/surrogate-1-harvest"
SVC_USER="ubuntu"
[ ! -d "/home/ubuntu/.ssh" ] && [ -d "/home/opc/.ssh" ] && SVC_USER="opc"

DAEMONS=(
    "dev:axentx-dev-daemon.py"
    "reviewer:axentx-reviewer-daemon.py"
    "qa:axentx-qa-daemon.py"
    "commit:axentx-commit-daemon.py"
    "pm:axentx-pm-daemon.py"
)

for entry in "${DAEMONS[@]}"; do
    role="${entry%%:*}"
    script="${entry##*:}"
    svc_name="axentx-${role}-daemon"
    desc=""
    case "$role" in
        dev) desc="produces project/focus tasks every 5 min" ;;
        reviewer) desc="consumes review-queue (continuous)" ;;
        qa) desc="consumes qa-queue, writes TDD tests (continuous)" ;;
        commit) desc="consumes commit-queue, pushes to GitHub (continuous)" ;;
        pm) desc="mini-sprint planning + retro every 4h" ;;
    esac

    cat > /etc/systemd/system/${svc_name}.service <<EOF
[Unit]
Description=axentx pipeline / ${role} — ${desc}
After=network-online.target surrogate-coordinator.service
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
WorkingDirectory=${REPO_ROOT}
EnvironmentFile=/etc/surrogate-coordinator.env
Environment=PYTHONUNBUFFERED=1
Environment=REPO_ROOT=${REPO_ROOT}
Environment=AXENTX_ROOT=/opt/axentx
ExecStart=${REPO_ROOT}/.venv/bin/python ${REPO_ROOT}/bin/${script}
Restart=always
RestartSec=15
# Resource limits — coordinator has 1 GB RAM, keep daemons light
MemoryMax=128M
TasksMax=8
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    echo "[install-daemons] ✓ ${svc_name}.service"
done

systemctl daemon-reload

# Enable + start all
for entry in "${DAEMONS[@]}"; do
    role="${entry%%:*}"
    svc_name="axentx-${role}-daemon"
    systemctl enable --now "${svc_name}.service" 2>&1 | tail -1
done

sleep 3
echo ""
echo "[install-daemons] status check:"
for entry in "${DAEMONS[@]}"; do
    role="${entry%%:*}"
    svc_name="axentx-${role}-daemon"
    state=$(systemctl is-active "${svc_name}.service" 2>&1)
    printf "  %-30s %s\n" "$svc_name" "$state"
done
