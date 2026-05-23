#!/usr/bin/env bash
# Generic dev-cloud daemon — always-on worker for a specific cloud provider.
# Usage via launchd: dev-cloud-daemon.sh <provider>
#   provider = github | samba | cloudflare | groq | gemini
# Pulls from same hermes:work:coding queue as qwen-coder-daemon.
# Worker lock namespaced by provider so 6 daemons process different priorities concurrently.
set -u

PROVIDER="${1:?usage: dev-cloud-daemon.sh <github|samba|cloudflare|groq|gemini>}"

LOG="$HOME/.surrogate/logs/dev-cloud-daemon-${PROVIDER}.log"
mkdir -p "$(dirname "$LOG")"

# Redis connection: prefer Unix socket, fall back to TCP 127.0.0.1:6379.
# REDIS_CLI_ARGS populates either "-s /path/to/socket" or "-h 127.0.0.1 -p 6379".
REDIS_SOCK=$(find /var/folders /tmp -name 'redis.socket' -type s 2>/dev/null | head -1)
if [[ -n "$REDIS_SOCK" ]] && [[ -S "$REDIS_SOCK" ]]; then
    REDIS_CLI_ARGS=(-s "$REDIS_SOCK")
elif redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG; then
    REDIS_CLI_ARGS=(-h 127.0.0.1 -p 6379)
else
    echo "[$(date '+%H:%M:%S')] no redis (socket or tcp:6379) — sleep 60s" >> "$LOG"
    sleep 60; exit 0
fi

echo "[$(date '+%H:%M:%S')] $PROVIDER daemon start (PID $$) via ${REDIS_CLI_ARGS[*]}" >> "$LOG"

while true; do
    # Budget-aware: check token budget before processing
    BUDGET_FILE="$HOME/.hermes/workspace/budget/tokens-$(/bin/date +%Y-%m-%d).json"
    if [[ -f "$BUDGET_FILE" ]]; then
        STATUS=$(python3 -c "
import json
try:
    d = json.load(open('$BUDGET_FILE'))
    print(d.get('providers',{}).get('$PROVIDER',{}).get('status','OK'))
except: print('OK')" 2>/dev/null)
        if [[ "$STATUS" == "HALT" ]]; then
            echo "[$(date '+%H:%M:%S')] $PROVIDER budget HALT — sleep 15 min" >> "$LOG"
            sleep 900; continue
        fi
    fi

    # BLPOP own provider list (fan-out: each provider has own list for parallel processing)
    RESULT=$(redis-cli "${REDIS_CLI_ARGS[@]}" BLPOP "hermes:work:coding:$PROVIDER" 30 2>/dev/null)
    [[ -z "$RESULT" ]] && continue

    PAYLOAD=$(echo "$RESULT" | tail -1)
    [[ -z "$PAYLOAD" ]] && continue

    PRIO_ID=$(echo "$PAYLOAD" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])" 2>/dev/null)
    [[ -z "$PRIO_ID" ]] && continue

    # Worker lock (provider-specific so 6 daemons can work in parallel on same queue)
    LOCK_KEY="hermes:worker-lock:$PRIO_ID:$PROVIDER"
    LOCK_ACQUIRED=$(redis-cli "${REDIS_CLI_ARGS[@]}" SET "$LOCK_KEY" "$PROVIDER" NX EX 900 2>/dev/null)
    if [[ "$LOCK_ACQUIRED" != "OK" ]]; then
        echo "[$(date '+%H:%M:%S')] $PRIO_ID: $PROVIDER lock exists — skip (30-min cooldown)" >> "$LOG"
        continue
    fi

    echo "[$(date '+%H:%M:%S')] $PROVIDER pulled $PRIO_ID" >> "$LOG"
    START=$(date +%s)
    # Pass the pinned priority so the worker bypasses its file-lock selection
    # and works on exactly what the daemon locked (avoids "no free priority"
    # dead-ends when the file lock was touched earlier for this same PRIO_ID).
    HERMES_PRIO_ID="$PRIO_ID" \
        "$HOME/.surrogate/bin/dev-cloud-worker.sh" "$PROVIDER" 2>&1 | tail -3 >> "$LOG"
    RC=${PIPESTATUS[0]}
    DUR=$(( $(date +%s) - START ))
    echo "[$(date '+%H:%M:%S')] $PROVIDER $PRIO_ID done in ${DUR}s (rc=$RC)" >> "$LOG"

    # Discord: only notify failures + slow tasks (avoid spam on every success)
    if [[ $RC -ne 0 ]]; then
        "$HOME/.surrogate/bin/notify-discord.sh" error "Worker failed" "$PROVIDER · $PRIO_ID · ${DUR}s · rc=$RC" 2>/dev/null &
    elif [[ $DUR -gt 240 ]]; then
        "$HOME/.surrogate/bin/notify-discord.sh" warn "Slow task" "$PROVIDER · $PRIO_ID · ${DUR}s" 2>/dev/null &
    fi
done
