#!/usr/bin/env bash
# Install hermes-scheduler + N hermes-worker daemons.
# Replaces the cron-tick surrogate-coordinator with continuous work-queue.
#
# Worker count tuned to e2-micro 1 GB RAM:
#   3 workers × ~30 MB Python = 90 MB
#   + scheduler 30 MB
#   + 5 axentx-* daemons × 30 MB = 150 MB
#   + watchdog + self-heal = 60 MB
#   = ~330 MB total daemon memory, leaving ~600 MB for OS + sshd + buffer
set -euo pipefail

REPO_ROOT="/opt/surrogate-1-harvest"
SVC_USER="ubuntu"
[ ! -d "/home/ubuntu/.ssh" ] && [ -d "/home/opc/.ssh" ] && SVC_USER="opc"
N_WORKERS="${N_WORKERS:-3}"

echo "[install-workqueue] target user: $SVC_USER"
echo "[install-workqueue] worker count: $N_WORKERS"

# Stop the old cron-tick coordinator first (it would race against scheduler).
sudo systemctl stop surrogate-coordinator 2>/dev/null || true
sudo systemctl disable surrogate-coordinator 2>/dev/null || true

# Scheduler service
sudo tee /etc/systemd/system/hermes-scheduler-daemon.service >/dev/null <<EOF
[Unit]
Description=hermes-scheduler — continuous job evaluator (replaces cron-tick coordinator)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
WorkingDirectory=${REPO_ROOT}
EnvironmentFile=/etc/surrogate-coordinator.env
Environment=PYTHONUNBUFFERED=1
Environment=REPO_ROOT=${REPO_ROOT}
ExecStart=${REPO_ROOT}/.venv/bin/python ${REPO_ROOT}/bin/hermes-scheduler-daemon.py
Restart=always
RestartSec=15
MemoryMax=64M
TasksMax=8
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Worker template — instantiate as hermes-worker-daemon@1, @2, @3
sudo tee /etc/systemd/system/hermes-worker-daemon@.service >/dev/null <<EOF
[Unit]
Description=hermes-worker %i — pulls from pending/ queue, executes
After=network-online.target hermes-scheduler-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
WorkingDirectory=${REPO_ROOT}
EnvironmentFile=/etc/surrogate-coordinator.env
Environment=PYTHONUNBUFFERED=1
Environment=REPO_ROOT=${REPO_ROOT}
Environment=WORKER_ID=%i
ExecStart=${REPO_ROOT}/.venv/bin/python ${REPO_ROOT}/bin/hermes-worker-daemon.py
Restart=always
RestartSec=15
MemoryMax=64M
TasksMax=8
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo chown -R "$SVC_USER:$SVC_USER" "$REPO_ROOT"
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-scheduler-daemon.service
for i in $(seq 1 "$N_WORKERS"); do
    sudo systemctl enable --now "hermes-worker-daemon@${i}.service"
done

sleep 4
echo ""
echo "[install-workqueue] status:"
printf "  %-32s %s\n" "hermes-scheduler-daemon" "$(sudo systemctl is-active hermes-scheduler-daemon)"
for i in $(seq 1 "$N_WORKERS"); do
    printf "  %-32s %s\n" "hermes-worker-daemon@${i}" "$(sudo systemctl is-active hermes-worker-daemon@${i})"
done
echo ""
echo "[install-workqueue] queue dirs:"
ls -la "${REPO_ROOT}/state/hermes-tasks/" 2>/dev/null || echo "  (will be created on first run)"
