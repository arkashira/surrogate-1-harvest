#!/usr/bin/env bash
# kamatera-provision.sh — provision a Kamatera VM during the 1MONTH300 promo.
#
# CONTEXT (2026-05-02):
#   Promo code 1MONTH300 grants 30 days free up to:
#     - 1 Cloud Server (up to $100 value)
#     - 1000 GB Cloud Block Storage
#     - 1000 GB Outgoing Internet Traffic
#   After 30 days, billing kicks in automatically. We MUST terminate the
#   server before day 30 — see kamatera-auto-terminate.sh (LaunchAgent
#   fires on day 28, well before billing).
#
# Usage:
#   bin/kamatera-provision.sh
#
# Required env (or in ~/.kam):
#   KAM_CLIENT_ID, KAM_SECRET
#
set -euo pipefail

LOG_PREFIX="[kamatera-provision]"
log() { echo "$LOG_PREFIX $(date -u +%H:%M:%SZ) $*"; }

# Load credentials
if [ -f "$HOME/.kam" ]; then
    KAM_CLIENT_ID=$(grep -oE "Access Key.*[a-f0-9]{32}|^[a-f0-9]{32}" "$HOME/.kam" | tail -1 | grep -oE "[a-f0-9]{32}")
    KAM_SECRET=$(grep -oE "Secret Key.*[a-f0-9]{32}|^[a-f0-9]{32}" "$HOME/.kam" | head -1 | grep -oE "[a-f0-9]{32}")
fi
KAM_CLIENT_ID="${KAM_CLIENT_ID:-${CLOUDCLI_APICLIENTID:-}}"
KAM_SECRET="${KAM_SECRET:-${CLOUDCLI_APISECRET:-}}"
if [ -z "$KAM_CLIENT_ID" ] || [ -z "$KAM_SECRET" ]; then
    log "missing creds — set KAM_CLIENT_ID + KAM_SECRET (or write to ~/.kam)"
    exit 1
fi
export CLOUDCLI_APICLIENTID="$KAM_CLIENT_ID"
export CLOUDCLI_APISECRET="$KAM_SECRET"

CLOUDCLI="${CLOUDCLI:-/tmp/cloudcli-bin}"
if [ ! -x "$CLOUDCLI" ]; then
    log "cloudcli not at $CLOUDCLI — building from source"
    cd /tmp && rm -rf cloudcli && git clone --depth=1 https://github.com/cloudwm/cloudcli.git
    cd /tmp/cloudcli && go build -o /tmp/cloudcli-bin . && cd -
    CLOUDCLI=/tmp/cloudcli-bin
fi

# ── Configuration — fits comfortably under $100/30d budget ────────────────
DATACENTER="${KAM_DATACENTER:-AS-SG}"      # Singapore (latency to user)
CPU="${KAM_CPU:-4B}"                        # 4 burstable cores
RAM="${KAM_RAM:-16384}"                     # 16 GB
DISK="${KAM_DISK:-100}"                     # 100 GB (well under 1000 GB promo)
NAME="${KAM_VM_NAME:-surrogate-watchdog-kamatera}"
SSH_PUB="${SSH_PUB:-$HOME/.ssh/oci-surrogate.pub}"
IMAGE_ID="${KAM_IMAGE:-}"                   # auto-resolve below
PASSWORD="${KAM_PASSWORD:-Surr0gate$(openssl rand -hex 4 | tr -d '\n')!1}"
TAG="surrogate-promo-1month300"

# Resolve bare Ubuntu 22.04 image if not set
if [ -z "$IMAGE_ID" ]; then
    log "resolving bare Ubuntu 22.04 image for $DATACENTER…"
    IMAGE_ID=$("$CLOUDCLI" server options --image 2>/dev/null \
        | grep -E "^${DATACENTER}:" \
        | grep -iE "ubuntuserver-22\\.04|ubuntu-22\\.04|server_ubuntu_22\\.04" \
        | grep -viE "apps_|service_|^[^:]+:[^[:space:]]+[[:space:]]+apps" \
        | head -1 | awk '{print $1}')
    if [ -z "$IMAGE_ID" ]; then
        log "couldn't resolve bare Ubuntu 22.04 — falling back to Ubuntu 24.04"
        IMAGE_ID=$("$CLOUDCLI" server options --image 2>/dev/null \
            | grep -E "^${DATACENTER}:" \
            | grep -iE "ubuntuserver-24\\.04" \
            | grep -viE "apps_|service_" \
            | head -1 | awk '{print $1}')
    fi
fi
[ -z "$IMAGE_ID" ] && { log "✗ no Ubuntu image found in $DATACENTER"; exit 1; }
log "image: $IMAGE_ID"

# Generate SSH key if missing (re-use OCI key if present)
if [ ! -f "$SSH_PUB" ]; then
    log "generating SSH key $SSH_PUB"
    ssh-keygen -t ed25519 -N "" -f "${SSH_PUB%.pub}" -C "axentx-kamatera"
fi

log "provisioning: $NAME ($CPU CPU, ${RAM}MB RAM, ${DISK}GB disk, $DATACENTER)"
log "  estimated cost: ~\$73-85/mo (well under \$100 promo cap)"

CREATE_OUT=$("$CLOUDCLI" server create \
    --name "$NAME" \
    --datacenter "$DATACENTER" \
    --image "$IMAGE_ID" \
    --cpu "$CPU" \
    --ram "$RAM" \
    --disk "id=0,size=$DISK" \
    --network "id=0,name=wan,ip=auto" \
    --password "$PASSWORD" \
    --billingcycle hourly \
    --poweronaftercreate yes \
    --ssh-key "$SSH_PUB" \
    --tag "$TAG" \
    --wait 2>&1)
echo "$CREATE_OUT" | tail -15

# Extract created server ID
SERVER_ID=$(echo "$CREATE_OUT" | grep -oE "[0-9a-f]{32}" | head -1)
if [ -z "$SERVER_ID" ]; then
    log "✗ couldn't parse server ID from create output"
    exit 2
fi

# Get IP
sleep 6
IP=$("$CLOUDCLI" server list 2>&1 | awk -v n="$NAME" '$2==n {print $5; exit}')
log "✓ provisioned $SERVER_ID  IP=$IP"

# Persist metadata for auto-terminate to use
mkdir -p "$HOME/.surrogate"
cat > "$HOME/.surrogate/kamatera-server.json" <<EOF
{
  "server_id": "$SERVER_ID",
  "name": "$NAME",
  "ip": "$IP",
  "datacenter": "$DATACENTER",
  "cpu": "$CPU",
  "ram_mb": $RAM,
  "disk_gb": $DISK,
  "promo_code": "1MONTH300",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "auto_terminate_at": "$(date -u -v+28d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+28 days' +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
log "metadata: ~/.surrogate/kamatera-server.json"
log "auto-terminate scheduled: $(date -u -v+28d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+28 days' +%Y-%m-%dT%H:%M:%SZ)"
log ""
log "Next steps:"
log "  1. ssh -i ${SSH_PUB%.pub} ubuntu@$IP    (after ~30s for SSH to come up)"
log "  2. bash $HOME/develope/axentx-bin/kamatera-vm-bootstrap.sh on the VM"
log "  3. install LaunchAgent: launchctl load ~/Library/LaunchAgents/com.axentx.kamatera-auto-terminate.plist"
