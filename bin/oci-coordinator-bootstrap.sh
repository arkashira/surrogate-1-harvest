#!/usr/bin/env bash
# OCI surrogate-coordinator bootstrap.
# Replaces dead Mac hermes-gateway daemon (stopped 2026-04-27).
#
# What it installs:
#   - Python 3.11 + deps (huggingface_hub, urllib3)
#   - Repo clone /opt/surrogate-1-harvest
#   - systemd service: surrogate-coordinator.service
#       Polls data/hermes-jobs.json every 60s, dispatches due jobs
#       True 24x7 (vs GH Actions every-5-min tick)
#   - Discord heartbeat: status to webhook every 10 min
#
# Usage (run on the OCI instance as ubuntu/opc):
#   curl -sSL https://raw.githubusercontent.com/arkashira/surrogate-1-harvest/main/bin/oci-coordinator-bootstrap.sh \
#     | sudo bash
#
# Required env (set in /etc/surrogate-coordinator.env after install):
#   CEREBRAS_API_KEY GROQ_API_KEY OPENROUTER_API_KEY ANTHROPIC_API_KEY HF_TOKEN DISCORD_WEBHOOK
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/arkashira/surrogate-1-harvest.git}"
INSTALL_DIR="/opt/surrogate-1-harvest"
# Detect the right user for the OS image. Both Oracle Linux (opc) and
# Ubuntu (ubuntu) directories may exist on the same image; prefer ubuntu
# when /home/ubuntu has the cloud-init authorized_keys baked in.
SVC_USER="ubuntu"
if [ ! -d "/home/ubuntu/.ssh" ] && [ -d "/home/opc/.ssh" ]; then
    SVC_USER="opc"
fi
ENV_FILE="/etc/surrogate-coordinator.env"

echo "[bootstrap] target user: $SVC_USER"
echo "[bootstrap] repo:        $REPO_URL"
echo "[bootstrap] install dir: $INSTALL_DIR"

# 1. Install OS deps
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
    git python3 python3-pip python3-venv ca-certificates curl jq

# 2. Clone repo
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR"

# 3. Python venv + deps
sudo -u "$SVC_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SVC_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SVC_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet \
    huggingface_hub requests

# 4. Env file (operator fills in actual values)
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'ENVEOF'
# OCI surrogate-coordinator runtime env
CEREBRAS_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
ANTHROPIC_API_KEY=
HF_TOKEN=
DISCORD_WEBHOOK=
TICK_INTERVAL_SEC=300
ENVEOF
    chmod 600 "$ENV_FILE"
    echo "[bootstrap] ⚠ created $ENV_FILE — fill in keys, then 'sudo systemctl restart surrogate-coordinator'"
fi

# 5. systemd service
cat > /etc/systemd/system/surrogate-coordinator.service << EOF
[Unit]
Description=Surrogate-1 Coordinator (hermes-cron dispatcher, 60s tick)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python -u $INSTALL_DIR/bin/oci-coordinator-loop.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 6. Loop wrapper script
cat > "$INSTALL_DIR/bin/oci-coordinator-loop.py" << 'LOOPEOF'
#!/usr/bin/env python3
"""Coordinator loop — runs hermes-cron-dispatcher.py every TICK_INTERVAL_SEC."""
import os, time, subprocess, sys, signal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TICK = int(os.environ.get("TICK_INTERVAL_SEC", "60"))

def shutdown(*_): print("[loop] shutting down", flush=True); sys.exit(0)
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

print(f"[loop] start — tick every {TICK}s", flush=True)
while True:
    t0 = time.time()
    try:
        subprocess.run(
            [sys.executable, str(REPO / "bin/hermes-cron-dispatcher.py")],
            check=False, timeout=TICK - 5,
        )
    except subprocess.TimeoutExpired:
        print(f"[loop] dispatcher exceeded {TICK-5}s — killing", flush=True)
    elapsed = time.time() - t0
    sleep_for = max(5, TICK - elapsed)
    time.sleep(sleep_for)
LOOPEOF
chown "$SVC_USER:$SVC_USER" "$INSTALL_DIR/bin/oci-coordinator-loop.py"
chmod +x "$INSTALL_DIR/bin/oci-coordinator-loop.py"

# 7. Enable + start
systemctl daemon-reload
systemctl enable surrogate-coordinator.service
# Don't auto-start until env is filled — operator runs:
#   sudo systemctl start surrogate-coordinator

echo "[bootstrap] ✓ installed surrogate-coordinator.service (NOT started yet)"
echo "[bootstrap] next steps:"
echo "  1. edit $ENV_FILE — fill in API keys"
echo "  2. sudo systemctl start surrogate-coordinator"
echo "  3. journalctl -u surrogate-coordinator -f"
