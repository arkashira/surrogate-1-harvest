#!/usr/bin/env bash
# oci-provision-a1.sh — provision OCI Always Free ARM A1 (or x86 fallback)
#
# User directive (2026-05-02):
#   > "OCI เค้าแก้ปัญหาให้แล้ว — เช็ค A1 instance ก่อนว่าว่างไหม ถ้าว่าง
#   >  เอาเลย ถ้าไม่ ก็ไล่เอาที่ฟรีเอามาใช้ได้ แล้วให้ทำงานพร้อมกันกับ
#   >  ฝั่ง GCP เลย"
#
# Strategy:
#   1. Try ARM Ampere A1.Flex (4 OCPU / 24GB free tier — beefiest free anywhere).
#   2. If A1 capacity error, fall back to x86 VM.Standard.E2.1.Micro (1/1, free).
#   3. After provisioning, call surrogate-bootstrap.sh on the new VM to install
#      the daemon fleet + state branch checkout. New VM joins /dash/agents
#      automatically (each daemon already heartbeats with HOSTNAME tag).
#
# Prereqs (run on operator's laptop):
#   - oci CLI configured: `oci session authenticate --profile-name ashirafuse`
#   - SSH key: ~/.ssh/oci-surrogate.pub (we generate if missing)
#
# Usage:
#   bin/oci-provision-a1.sh                     # use defaults
#   COMPARTMENT_ID=ocid1...  bin/oci-provision-a1.sh
#
set -euo pipefail

PROFILE="${OCI_PROFILE:-ashirafuse}"
AUTH="--profile $PROFILE --auth security_token"
NAME="${VM_NAME:-surrogate-watchdog-oci}"
REGION="${OCI_REGION:-ap-singapore-1}"
SSH_KEY_PUB="${SSH_KEY_PUB:-$HOME/.ssh/oci-surrogate.pub}"
SSH_KEY_PRIV="${SSH_KEY_PUB%.pub}"
IMAGE_OS="${OS_IMAGE:-Canonical Ubuntu}"
IMAGE_VER="${OS_VER:-22.04}"
LOG_PREFIX="[oci-provision]"

log() { echo "$LOG_PREFIX $(date -u +%H:%M:%SZ) $*"; }

# ── 0. Pre-flight ─────────────────────────────────────────────────────────
if ! command -v oci >/dev/null; then
  echo "$LOG_PREFIX oci CLI not installed — brew install oci-cli"; exit 1
fi
if ! oci $AUTH iam region list >/dev/null 2>&1; then
  echo "$LOG_PREFIX session expired — run: oci session authenticate --profile-name $PROFILE"
  exit 1
fi
if [ ! -f "$SSH_KEY_PUB" ]; then
  log "generating SSH key $SSH_KEY_PRIV"
  ssh-keygen -t ed25519 -N "" -f "$SSH_KEY_PRIV" -C "axentx-surrogate@oci"
fi

# ── 1. Resolve compartment + AD ───────────────────────────────────────────
COMPARTMENT_ID="${COMPARTMENT_ID:-$(oci $AUTH iam compartment list \
  --all --query 'data[?\"lifecycle-state\"==`ACTIVE`].id | [0]' --raw-output)}"
if [ -z "$COMPARTMENT_ID" ]; then
  log "could not resolve compartment id — set COMPARTMENT_ID env var"; exit 1
fi
log "compartment: $COMPARTMENT_ID"

ADs=$(oci $AUTH iam availability-domain list \
  --compartment-id "$COMPARTMENT_ID" \
  --query 'data[].name' --raw-output)
log "ADs found: $(echo "$ADs" | tr '\n' ' ')"

# ── 2. Resolve image + subnet ─────────────────────────────────────────────
log "resolving Ubuntu 22.04 ARM image"
IMG_ARM=$(oci $AUTH compute image list \
  --compartment-id "$COMPARTMENT_ID" \
  --operating-system "$IMAGE_OS" \
  --operating-system-version "$IMAGE_VER" \
  --shape "VM.Standard.A1.Flex" \
  --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0].id' --raw-output)
IMG_X86=$(oci $AUTH compute image list \
  --compartment-id "$COMPARTMENT_ID" \
  --operating-system "$IMAGE_OS" \
  --operating-system-version "$IMAGE_VER" \
  --shape "VM.Standard.E2.1.Micro" \
  --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0].id' --raw-output)
log "ARM image: $IMG_ARM"
log "x86 image: $IMG_X86"

# Pick first VCN + public subnet in compartment (assumes one exists; user
# can pre-create one via Console if absent).
SUBNET_ID=$(oci $AUTH network subnet list \
  --compartment-id "$COMPARTMENT_ID" \
  --query 'data[?"prohibit-public-ip-on-vnic"==`false`].id | [0]' \
  --raw-output)
if [ -z "$SUBNET_ID" ] || [ "$SUBNET_ID" = "null" ]; then
  log "no public subnet found — falling back to any subnet"
  SUBNET_ID=$(oci $AUTH network subnet list \
    --compartment-id "$COMPARTMENT_ID" \
    --query 'data[0].id' --raw-output)
fi
log "subnet: $SUBNET_ID"

# ── 3. Try A1 Flex first (4 OCPU / 24 GB), fall back to x86 ───────────────
try_launch() {
  local shape="$1" image="$2" cfg="$3" ad="$4"
  log "▸ launch $shape on $ad"
  oci $AUTH compute instance launch \
    --compartment-id "$COMPARTMENT_ID" \
    --availability-domain "$ad" \
    --shape "$shape" \
    --shape-config "$cfg" \
    --image-id "$image" \
    --subnet-id "$SUBNET_ID" \
    --display-name "$NAME" \
    --assign-public-ip true \
    --ssh-authorized-keys-file "$SSH_KEY_PUB" \
    --wait-for-state RUNNING \
    --query 'data.id' --raw-output 2>&1 | tee /tmp/oci-launch-$$.log
}

INSTANCE_ID=""
for ad in $ADs; do
  if [ -n "$IMG_ARM" ] && [ "$IMG_ARM" != "null" ]; then
    if INSTANCE_ID=$(try_launch "VM.Standard.A1.Flex" "$IMG_ARM" \
        '{"ocpus":4,"memoryInGBs":24}' "$ad" 2>&1 | tail -1); then
      if [[ "$INSTANCE_ID" == ocid1.instance.* ]]; then
        log "✓ A1.Flex provisioned: $INSTANCE_ID"
        SHAPE_USED="VM.Standard.A1.Flex(4/24)"
        break
      fi
    fi
    log "  A1 unavailable on $ad — trying next AD or fallback"
  fi
done

if [ -z "$INSTANCE_ID" ] || [[ "$INSTANCE_ID" != ocid1.instance.* ]]; then
  log "A1 unavailable across all ADs — falling back to x86 E2.1.Micro"
  for ad in $ADs; do
    if INSTANCE_ID=$(try_launch "VM.Standard.E2.1.Micro" "$IMG_X86" \
        '{}' "$ad" 2>&1 | tail -1); then
      if [[ "$INSTANCE_ID" == ocid1.instance.* ]]; then
        log "✓ E2.1.Micro provisioned: $INSTANCE_ID"
        SHAPE_USED="VM.Standard.E2.1.Micro"
        break
      fi
    fi
  done
fi

if [ -z "$INSTANCE_ID" ] || [[ "$INSTANCE_ID" != ocid1.instance.* ]]; then
  log "✗ no shape available — both A1 and E2 capacity errored. Try again later."
  exit 2
fi

PUBLIC_IP=$(oci $AUTH compute instance list-vnics \
  --instance-id "$INSTANCE_ID" \
  --query 'data[0]."public-ip"' --raw-output)
log "✓ public IP: $PUBLIC_IP  (shape=$SHAPE_USED)"
log "wait 30s for SSH to come up…"
sleep 30

# ── 4. Bootstrap the new VM (idempotent) ─────────────────────────────────
log "bootstrapping daemon fleet via SSH"
ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=~/.ssh/known_hosts.oci \
    -i "$SSH_KEY_PRIV" "ubuntu@$PUBLIC_IP" \
    'curl -fsSL https://raw.githubusercontent.com/arkashira/surrogate-1-harvest/main/bin/oci-vm-bootstrap.sh | sudo bash -s' || \
  log "  ⚠ bootstrap script not yet on main — push bin/oci-vm-bootstrap.sh next"

cat <<EOF
===========================================================================
$LOG_PREFIX OCI VM provisioned
  name:     $NAME
  shape:    $SHAPE_USED
  region:   $REGION
  ip:       $PUBLIC_IP
  ssh:      ssh -i $SSH_KEY_PRIV ubuntu@$PUBLIC_IP

Next:
  1. SSH in and verify state-sync pulled the state branch:
       sudo systemctl status surrogate-state-sync-daemon
  2. Watch /dash/agents — within 60s the new host should appear with
     'host=<new-vm-hostname>' next to existing GCP entries.
  3. To remove: oci compute instance terminate --instance-id $INSTANCE_ID
===========================================================================
EOF
