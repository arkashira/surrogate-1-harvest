#!/usr/bin/env bash
# kamatera-auto-terminate.sh — runs hourly via LaunchAgent. Terminates the
# Kamatera VM at day 28 of the 1MONTH300 promo, with Discord warnings at
# day 25 and day 27. Defense-in-depth against accidental billing.
#
# State file:  ~/.surrogate/kamatera-server.json   (written by provision script)
# Behavior:
#   day < 25         — silent
#   day 25 / 27      — Discord warning once per day
#   day 28           — terminate VM, post Discord, mark state file as terminated
#   day > 28         — refuse (already terminated; should never run)
#
set -euo pipefail

STATE_FILE="$HOME/.surrogate/kamatera-server.json"
LOG_FILE="$HOME/.surrogate/logs/kamatera-auto-terminate.log"
WARN_STAMP="$HOME/.surrogate/.kamatera-warn-stamps"
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$WARN_STAMP"

log() { echo "[$(date -u +%H:%MZ)] $*" | tee -a "$LOG_FILE"; }

if [ ! -f "$STATE_FILE" ]; then
    log "no server state file — nothing to do"
    exit 0
fi

if grep -q "\"terminated_at\"" "$STATE_FILE"; then
    log "already terminated — exit"
    exit 0
fi

CREATED=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d['created_at'])")
SERVER_ID=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d['server_id'])")
NAME=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('name',''))")

CREATED_EPOCH=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$CREATED" "+%s" 2>/dev/null || \
                 date -u -d "$CREATED" "+%s")
NOW_EPOCH=$(date -u "+%s")
AGE_DAYS=$(( (NOW_EPOCH - CREATED_EPOCH) / 86400 ))

log "server $NAME ($SERVER_ID): age=$AGE_DAYS days"

post_discord() {
    local msg="$1"
    local webhook="${DISCORD_WEBHOOK:-}"
    [ -z "$webhook" ] && return 0
    curl -fsS -X POST "$webhook" -H 'Content-Type: application/json' \
        -d "{\"content\":\"$msg\"}" >/dev/null 2>&1 || true
}

warn_once() {
    local day="$1" msg="$2"
    local stamp="$WARN_STAMP/day$day"
    [ -f "$stamp" ] && return 0
    log "▸ warning day $day"
    post_discord "$msg"
    touch "$stamp"
}

terminate_vm() {
    log "▸ terminating $NAME ($SERVER_ID)"
    if [ -f "$HOME/.kam" ]; then
        export CLOUDCLI_APICLIENTID=$(grep -oE "[a-f0-9]{32}" "$HOME/.kam" | tail -1)
        export CLOUDCLI_APISECRET=$(grep -oE "[a-f0-9]{32}" "$HOME/.kam" | head -1)
    fi
    out=$(/tmp/cloudcli-bin server terminate --id "$SERVER_ID" --force --wait 2>&1) || {
        log "  ✗ terminate failed: $(echo "$out" | tail -3)"
        post_discord "🚨 **Kamatera auto-terminate FAILED** — $NAME still running. MANUAL ACTION REQUIRED before promo expires!\n$(echo "$out" | tail -3)"
        return 1
    }
    log "  ✓ terminated"
    # mark state file
    python3 -c "
import json, datetime
d = json.load(open('$STATE_FILE'))
d['terminated_at'] = datetime.datetime.utcnow().isoformat() + 'Z'
json.dump(d, open('$STATE_FILE', 'w'), indent=2)
"
    post_discord "✅ **Kamatera auto-terminate** — $NAME terminated at day 28 (promo 1MONTH300 expires day 30, no charge)."
}

case $AGE_DAYS in
    25)
        warn_once 25 "⏰ **Kamatera promo day 25** — auto-terminate in 3 days. Server $NAME ($SERVER_ID). Free promo expires day 30, we kill at 28 for safety. Save any work."
        ;;
    26)
        ;;
    27)
        warn_once 27 "⚠️ **Kamatera promo day 27** — TERMINATING TOMORROW. Server $NAME. Pull anything you need from it now."
        ;;
    28|29|30|31)
        warn_once 28 "🛑 **Kamatera day $AGE_DAYS — AUTO-TERMINATING NOW**"
        terminate_vm
        ;;
    *)
        if [ "$AGE_DAYS" -gt 31 ]; then
            log "✗✗ over day 31 and STILL RUNNING — emergency"
            post_discord "🆘 **EMERGENCY** Kamatera $NAME age=$AGE_DAYS days. Promo EXPIRED. Manual terminate via console.kamatera.com NOW."
        fi
        ;;
esac
