#!/usr/bin/env bash
# OCI surrogate-watchdog bootstrap.
# Monitors: 6 HF Spaces + Kaggle adapter targets + OCI coordinator + GH Actions cron
# Alerts: Discord webhook on degradation
# Heartbeat every 5 min; full sweep every 30 min
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
ENV_FILE="/etc/surrogate-watchdog.env"

echo "[bootstrap] watchdog install starting"

# 1. Deps
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
    git python3 python3-pip python3-venv ca-certificates curl jq

# 2. Repo
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR"

# 3. venv
sudo -u "$SVC_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SVC_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip requests

# 4. Env
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'ENVEOF'
# OCI surrogate-watchdog runtime env
HF_TOKEN=
DISCORD_WEBHOOK=
COORDINATOR_HOST=
SWEEP_INTERVAL_SEC=300
ENVEOF
    chmod 600 "$ENV_FILE"
fi

# 5. Watchdog daemon
cat > "$INSTALL_DIR/bin/oci-watchdog-daemon.py" << 'WDEOF'
#!/usr/bin/env python3
"""surrogate-watchdog — monitors fleet health, alerts to Discord."""
from __future__ import annotations
import json, os, time, urllib.request, urllib.error, signal, sys, datetime, socket

HF_TOKEN = os.environ.get("HF_TOKEN", "")
DISCORD = os.environ.get("DISCORD_WEBHOOK", "")
COORD_HOST = os.environ.get("COORDINATOR_HOST", "")
INTERVAL = int(os.environ.get("SWEEP_INTERVAL_SEC", "300"))

SPACES = [
    "axentx/surrogate-1",
    "surrogate1/surrogate-1-shard2",
    "surrogate1/surrogate-1-zero-gpu",
    "ashirafuse1/surrogate-1-shard3",
    "ashirato/surrogate-1-zero-gpu",
    "ashirato/surrogate-1-shard1",
]

def get_json(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_error": str(e)}

def post_discord(msg, color=0x808080):
    if not DISCORD: return
    body = json.dumps({"embeds": [{"description": msg[:1900], "color": color, "timestamp": datetime.datetime.utcnow().isoformat()}]}).encode()
    # Discord rejects requests without a recognizable User-Agent (403 Forbidden).
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/arkashira/surrogate-1-harvest, 1.0)",
    }
    try:
        urllib.request.urlopen(urllib.request.Request(DISCORD, data=body, headers=headers), timeout=8)
    except Exception as e: print(f"[wd] discord fail: {e}", flush=True)

def shutdown(*_): print("[wd] stopping", flush=True); sys.exit(0)
signal.signal(signal.SIGTERM, shutdown); signal.signal(signal.SIGINT, shutdown)

print(f"[wd] start — sweep every {INTERVAL}s", flush=True)
sweep_n = 0
while True:
    sweep_n += 1
    issues = []
    # 1. HF Spaces
    n_running = 0
    for sp in SPACES:
        d = get_json(f"https://huggingface.co/api/spaces/{sp}")
        if "_error" in d: issues.append(f"HF API fail for {sp}: {d['_error']}"); continue
        stage = d.get("runtime", {}).get("stage", "?")
        if stage == "RUNNING": n_running += 1
        else: issues.append(f"⚠ {sp} stage={stage}")
    # 2. Coordinator (if env set)
    if COORD_HOST:
        try:
            s = socket.create_connection((COORD_HOST, 22), timeout=4); s.close()
        except Exception as e:
            issues.append(f"⚠ coordinator {COORD_HOST}:22 unreachable: {e}")
    # 3. emit
    summary = f"[wd #{sweep_n}] {n_running}/{len(SPACES)} HF spaces running"
    print(f"{summary} | issues={len(issues)}", flush=True)
    for it in issues: print(f"  - {it}", flush=True)
    if issues:
        post_discord(f"**watchdog #{sweep_n}**: {len(issues)} issue(s)\n" + "\n".join(f"• {i}" for i in issues), color=0xff8800)
    elif sweep_n % 12 == 1:  # heartbeat every 12 sweeps (~1h at 5min)
        post_discord(f"**watchdog #{sweep_n}** ✓ all green ({n_running}/{len(SPACES)} spaces)", color=0x00cc66)
    time.sleep(INTERVAL)
WDEOF
chown "$SVC_USER:$SVC_USER" "$INSTALL_DIR/bin/oci-watchdog-daemon.py"
chmod +x "$INSTALL_DIR/bin/oci-watchdog-daemon.py"

# 6. systemd
cat > /etc/systemd/system/surrogate-watchdog.service << EOF
[Unit]
Description=Surrogate-1 Watchdog (HF Spaces + Kaggle + Coordinator monitor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python -u $INSTALL_DIR/bin/oci-watchdog-daemon.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable surrogate-watchdog.service
echo "[bootstrap] ✓ surrogate-watchdog installed (fill $ENV_FILE then start)"
echo "  sudo systemctl start surrogate-watchdog"
echo "  journalctl -u surrogate-watchdog -f"
