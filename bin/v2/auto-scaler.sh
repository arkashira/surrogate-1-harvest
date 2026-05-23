#!/usr/bin/env bash
# Surrogate-1 v2 — adaptive worker auto-scaler.
#
# User: 'burst เลย แต่ห้ามตาย'
#
# Strategy: monitor MemAvailable every minute. Spawn streaming workers UP
# when memory is plentiful (burst mode); kill workers when memory tight.
# No fixed BULK/STREAM_WORKERS — fully dynamic.
#
# Memory tiers (cpu-basic 16 GB Space):
#   ≥10 GB free → BURST: target 4 streaming + 1 bulk worker (max)
#   ≥6 GB free  → MID:   target 3 streaming
#   ≥4 GB free  → SAFE:  target 2 streaming
#   ≥3 GB free  → MIN:   target 1 streaming (current default)
#   <3 GB free  → CRISIS: kill all workers, let cron tasks finish
#
# Each tick: read avail mem, count current workers, spawn/kill the diff.
# Kill order: bulk first (heaviest), then youngest streaming.
# Spawn always streaming first (lightest).
#
# Cron: M%5 (every 5 min, no minute=0 collide).
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

LOG="$HOME/.surrogate/logs/auto-scaler.log"
mkdir -p "$(dirname "$LOG")"

# Read MemAvailable
if [[ -r /proc/meminfo ]]; then
    AVAIL_MB=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
else
    AVAIL_MB=99999
fi

# Count current workers
N_BULK=$(pgrep -cf "bulk-mirror-worker.sh" 2>/dev/null || echo 0)
N_STREAM=$(pgrep -cf "streaming-mirror-worker.sh" 2>/dev/null || echo 0)

# Determine target by memory tier
if   (( AVAIL_MB >= 10000 )); then T_STREAM=4; T_BULK=1; TIER="BURST"
elif (( AVAIL_MB >= 6000  )); then T_STREAM=3; T_BULK=0; TIER="MID"
elif (( AVAIL_MB >= 4000  )); then T_STREAM=2; T_BULK=0; TIER="SAFE"
elif (( AVAIL_MB >= 3000  )); then T_STREAM=1; T_BULK=0; TIER="MIN"
else                                T_STREAM=0; T_BULK=0; TIER="CRISIS"
fi

ACTION=""

# CRISIS: kill everything
if [[ "$TIER" == "CRISIS" ]]; then
    pkill -f "bulk-mirror-worker.sh" 2>/dev/null && ACTION="${ACTION}killed-bulk "
    pkill -f "streaming-mirror-worker.sh" 2>/dev/null && ACTION="${ACTION}killed-stream "
fi

# Spawn streaming up to target
DIFF=$((T_STREAM - N_STREAM))
if (( DIFF > 0 )); then
    for _ in $(seq 1 "$DIFF"); do
        wid="autoscale-stream-$(date +%s)-$$-${RANDOM}"
        nohup bash "$HOME/.surrogate/bin/v2/streaming-mirror-worker.sh" "$wid" \
            > "$HOME/.surrogate/logs/stream-$wid.log" 2>&1 &
        ACTION="${ACTION}+stream "
    done
elif (( DIFF < 0 )); then
    # Kill youngest streaming first
    KILLN=$(( -DIFF ))
    pgrep -f "streaming-mirror-worker.sh" | tail -"$KILLN" | xargs -r kill 2>/dev/null
    ACTION="${ACTION}-${KILLN}stream "
fi

# Spawn bulk up to target
DIFF_B=$((T_BULK - N_BULK))
if (( DIFF_B > 0 )); then
    for _ in $(seq 1 "$DIFF_B"); do
        wid="autoscale-bulk-$(date +%s)-$$-${RANDOM}"
        nohup bash "$HOME/.surrogate/bin/v2/bulk-mirror-worker.sh" "$wid" \
            > "$HOME/.surrogate/logs/bulk-$wid.log" 2>&1 &
        ACTION="${ACTION}+bulk "
    done
elif (( DIFF_B < 0 )); then
    KILLN=$(( -DIFF_B ))
    pgrep -f "bulk-mirror-worker.sh" | tail -"$KILLN" | xargs -r kill 2>/dev/null
    ACTION="${ACTION}-${KILLN}bulk "
fi

[[ -z "$ACTION" ]] && ACTION="steady"

echo "[$(date '+%H:%M:%S')] tier=$TIER avail=${AVAIL_MB}MB stream=$N_STREAM/$T_STREAM bulk=$N_BULK/$T_BULK action=$ACTION" >> "$LOG"

# Discord notify on tier change (compare to last)
LAST_TIER_FILE="$HOME/.surrogate/logs/.auto-scaler-last-tier"
LAST_TIER=$(cat "$LAST_TIER_FILE" 2>/dev/null || echo "")
if [[ "$TIER" != "$LAST_TIER" ]]; then
    echo "$TIER" > "$LAST_TIER_FILE"
    if [[ -n "${DISCORD_WEBHOOK:-}" ]]; then
        case "$TIER" in
            BURST)  emoji="🚀" ;;
            MID)    emoji="⚡" ;;
            SAFE)   emoji="✅" ;;
            MIN)    emoji="⚠️" ;;
            CRISIS) emoji="🔴" ;;
        esac
        curl -s -X POST -H "Content-Type: application/json" \
            -d "{\"content\":\"$emoji auto-scaler: tier $LAST_TIER → **$TIER** (avail ${AVAIL_MB}MB) → stream=$T_STREAM bulk=$T_BULK\"}" \
            "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
    fi
fi
