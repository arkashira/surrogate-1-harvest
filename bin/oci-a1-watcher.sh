#!/usr/bin/env bash
# oci-a1-watcher.sh — keep retrying A1.Flex provisioning until capacity opens.
#
# Free-tier ARM Ampere A1 is famously hard to grab on demand — capacity opens
# in irregular windows. This script polls every POLL_MIN minutes and grabs the
# A1 instance the moment OCI accepts. Designed to run from an OCI account
# that's already been authenticated (oci session authenticate) OR from the
# OCI VM itself with instance-principal auth.
#
# Behavior:
#   - Stops successfully on first 'Out of host capacity' WIN (instance ID returned)
#   - Backs off on rate-limit ('Too many requests') by 5x interval
#   - Logs every attempt to logs/oci-a1-watcher.log
#   - Posts to Discord on win
set -euo pipefail

PROFILE="${OCI_PROFILE:-ashirafuse}"
COMPID="${OCI_COMPARTMENT:-ocid1.tenancy.oc1..aaaaaaaaifw3s4ktcm5yg7i6ltis64onh4vedjcgweznrffmailgwuqm7r4a}"
AD="${OCI_AD:-Nkat:AP-SINGAPORE-1-AD-1}"
SUBNET_ID="${OCI_SUBNET:-ocid1.subnet.oc1.ap-singapore-1.aaaaaaaawx3mmbrchwanp5mtvxqjcgrpkkvmzswnmeblg5squjc47mvgfyaq}"
OCPUS="${OCI_OCPUS:-4}"
MEM_GB="${OCI_MEM_GB:-24}"
NAME="${OCI_VM_NAME:-surrogate-watchdog-oci-a1}"
SSH_PUB="${SSH_PUB:-$HOME/.ssh/oci-surrogate.pub}"
POLL_MIN="${POLL_MIN:-15}"
LOG_FILE="${LOG_FILE:-$HOME/.surrogate/logs/oci-a1-watcher.log}"
DISCORD="${DISCORD_WEBHOOK:-}"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  echo "[$(date -u +%H:%MZ)] $*" | tee -a "$LOG_FILE"
}

post_discord() {
  [ -z "$DISCORD" ] && return
  curl -fsS -X POST "$DISCORD" -H 'Content-Type: application/json' \
    -d "{\"content\":\"$1\"}" >/dev/null 2>&1 || true
}

# Resolve current Ubuntu 22.04 ARM image (versions rotate)
resolve_image() {
  oci --profile "$PROFILE" --auth security_token compute image list \
    --compartment-id "$COMPID" \
    --operating-system "Canonical Ubuntu" \
    --operating-system-version "22.04" \
    --shape "VM.Standard.A1.Flex" \
    --sort-by TIMECREATED --sort-order DESC \
    --query 'data[0].id' --raw-output 2>/dev/null | tail -1
}

attempt() {
  local img="$1"
  oci --profile "$PROFILE" --auth security_token compute instance launch \
    --compartment-id "$COMPID" \
    --availability-domain "$AD" \
    --shape "VM.Standard.A1.Flex" \
    --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}" \
    --image-id "$img" \
    --subnet-id "$SUBNET_ID" \
    --display-name "$NAME" \
    --assign-public-ip true \
    --ssh-authorized-keys-file "$SSH_PUB" \
    --wait-for-state RUNNING \
    --query 'data.{id:id,ip:"public-ip"}' --output json 2>&1
}

interval=$((POLL_MIN * 60))
log "start — A1 watcher (${OCPUS}/${MEM_GB}, AD=$AD, every ${POLL_MIN} min)"
while true; do
  IMG=$(resolve_image)
  if [ -z "$IMG" ] || [ "$IMG" = "null" ]; then
    log "  ⚠ couldn't resolve A1 image — retry in $interval s"
    sleep "$interval"
    continue
  fi
  log "▸ attempt (image $IMG)"
  set +e
  RESULT=$(attempt "$IMG" 2>&1)
  rc=$?
  set -e
  if [ $rc -eq 0 ] && echo "$RESULT" | grep -q "ocid1.instance"; then
    INSTANCE_ID=$(echo "$RESULT" | grep -oE "ocid1\\.instance\\.[a-zA-Z0-9.-]+" | head -1)
    PUB_IP=$(echo "$RESULT" | grep -oE '"ip":\\s*"[0-9.]+"' | grep -oE "[0-9.]+\\." | head -1 || echo "")
    log "✓ A1 PROVISIONED — $INSTANCE_ID  ip=$PUB_IP"
    post_discord "🎉 OCI A1.Flex provisioned at last! $INSTANCE_ID (${OCPUS}/${MEM_GB})"
    echo "$INSTANCE_ID" > "$HOME/.surrogate/oci-a1-instance-id"
    exit 0
  fi
  if echo "$RESULT" | grep -qi "too many requests"; then
    backoff=$((interval * 5))
    log "  rate-limited — backing off ${backoff} s"
    sleep "$backoff"
    continue
  fi
  if echo "$RESULT" | grep -qi "Out of host capacity"; then
    log "  capacity unavailable — retry in $interval s"
  else
    log "  unknown error: $(echo "$RESULT" | head -3 | tr '\n' ' ' | head -c 200)"
  fi
  sleep "$interval"
done
