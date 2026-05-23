#!/usr/bin/env bash
# Self-heal monitor — replaces the dead Mac watchdog.sh that
# committed suicide on 5-cascade error. This one heals instead.
#
# Triggered by hermes-cron-dispatcher every 5 min.
# Checks systemd services on coordinator + watchdog hosts;
# restarts any that are dead, with exponential backoff.
# Posts to Discord on actual recovery (not just heartbeat).
set -euo pipefail

REPO_ROOT="/opt/surrogate-1-harvest"
STATE_DIR="$REPO_ROOT/state/self-heal"
mkdir -p "$STATE_DIR"

declare -A SERVICES=(
    [surrogate-coordinator]="hermes-cron dispatcher"
    [surrogate-watchdog]="fleet monitor"
    [axentx-sprint-ceremony.timer]="sprint ceremonies"
)

post_discord() {
    [ -z "${DISCORD_WEBHOOK:-}" ] && return
    curl -sS --max-time 6 \
        -H "Content-Type: application/json" \
        -H "User-Agent: DiscordBot (https://github.com/arkashira/surrogate-1-harvest, 1.0)" \
        -d "{\"content\":\"$1\"}" \
        "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
}

restarted_any=0
for svc in "${!SERVICES[@]}"; do
    state_file="$STATE_DIR/${svc//\//_}.attempts"
    attempts=$(cat "$state_file" 2>/dev/null || echo "0")

    if systemctl is-active --quiet "$svc"; then
        # healthy → reset attempts counter
        echo "0" > "$state_file"
    else
        attempts=$((attempts + 1))
        echo "$attempts" > "$state_file"

        # exponential backoff: skip restart on attempts 6, 7, 8 (cool-down)
        if [ "$attempts" -gt 5 ] && [ "$attempts" -lt 9 ]; then
            echo "[self-heal] $svc dead (attempt $attempts) — backing off, skipping restart"
            continue
        fi

        # actually restart
        echo "[self-heal] $svc dead (attempt $attempts) — restart"
        if systemctl restart "$svc" 2>&1; then
            restarted_any=1
            sleep 3
            if systemctl is-active --quiet "$svc"; then
                echo "[self-heal] $svc ✓ recovered after $attempts attempt(s)"
                post_discord "🔧 self-heal: \`$svc\` restarted ok (was down for $attempts ticks)"
                echo "0" > "$state_file"
            else
                echo "[self-heal] $svc ✗ restart did not stick"
                if [ "$attempts" -ge 9 ]; then
                    post_discord "🚨 self-heal: \`$svc\` will not recover (attempt $attempts) — please investigate"
                fi
            fi
        fi
    fi
done

[ "$restarted_any" = 0 ] && echo "[self-heal] all services healthy"
exit 0
