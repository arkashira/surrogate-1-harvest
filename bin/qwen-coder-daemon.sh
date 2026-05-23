#!/usr/bin/env bash
# Qwen-coder daemon — always-on worker that BLPOPs from work queue.
# Run via launchd (never exits; launchd respawns if crash).
# Pulls priority → invokes qwen-coder-worker.sh with pre-selected priority (env var).
set -u

LOG="$HOME/.surrogate/logs/qwen-coder-daemon.log"
mkdir -p "$(dirname "$LOG")"

# Resolve Redis: Unix socket → TCP fallback. Build a redis-cli arg array reused below.
REDIS_SOCK=$(find /var/folders /tmp -name 'redis.socket' -type s 2>/dev/null | head -1)
if [[ -n "$REDIS_SOCK" ]] && [[ -S "$REDIS_SOCK" ]]; then
    RCLI=(redis-cli -s "$REDIS_SOCK")
elif redis-cli -h 127.0.0.1 -p 6379 PING 2>/dev/null | grep -q PONG; then
    RCLI=(redis-cli -h 127.0.0.1 -p 6379)
else
    echo "[$(date '+%H:%M:%S')] no redis (sock or TCP) — sleeping 60s before retry" >> "$LOG"
    sleep 60
    exit 0  # launchd will relaunch
fi

echo "[$(date '+%H:%M:%S')] daemon start (PID $$, mode=${RCLI[1]})" >> "$LOG"

# Main loop — pulls + processes until BLPOP times out (30s empty = exit, launchd relaunches)
while true; do
    RESULT=$("${RCLI[@]}" BLPOP 'hermes:work:coding:qwen-local' 30 2>/dev/null)
    [[ -z "$RESULT" ]] && continue

    PAYLOAD=$(echo "$RESULT" | tail -1)
    [[ -z "$PAYLOAD" ]] && continue

    PRIO_ID=$(echo "$PAYLOAD" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])" 2>/dev/null)
    [[ -z "$PRIO_ID" ]] && continue

    echo "[$(date '+%H:%M:%S')] pulled $PRIO_ID — processing" >> "$LOG"

    LOCK_KEY="hermes:worker-lock:$PRIO_ID:qwen-local"
    LOCK_ACQUIRED=$("${RCLI[@]}" SET "$LOCK_KEY" "qwen-local" NX EX 900 2>/dev/null)
    if [[ "$LOCK_ACQUIRED" != "OK" ]]; then
        echo "[$(date '+%H:%M:%S')] $PRIO_ID locked by another — skip" >> "$LOG"
        continue
    fi

    # Execute qwen-coder-worker with this priority pinned via env var so it
    # can't race with other workers / stale file locks.
    START=$(date +%s)
    HERMES_PRIO_ID="$PRIO_ID" \
        "$HOME/.surrogate/bin/qwen-coder-worker.sh" 2>&1 | tail -3 >> "$LOG"
    DUR=$(( $(date +%s) - START ))
    echo "[$(date '+%H:%M:%S')] $PRIO_ID done in ${DUR}s" >> "$LOG"

    # Release lock (TTL will also expire naturally)
    # Note: keep lock until done so crashes don't re-process
done

echo "[$(date '+%H:%M:%S')] daemon exit — launchd will relaunch" >> "$LOG"
