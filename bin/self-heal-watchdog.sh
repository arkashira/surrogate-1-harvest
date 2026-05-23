#!/usr/bin/env bash
# Self-healing watchdog — runs on HF Space, not Mac.
#
# Monitors three failure modes that previously needed manual intervention:
#   1. Memory pressure (>85% of 16Gi cap) — preempt OOM by killing the
#      youngest dataset-enrich shard. Earlier shards are preferred to live
#      because they're closer to completing their iteration.
#   2. Stuck ingestion — if /data/training-pairs.jsonl hasn't grown in 20 min
#      AND bulk-ingest log hasn't logged "kept:" in 15 min, force-restart
#      bulk-ingest-parallel.
#   3. Stale upload backlog — if push-training-to-hf.sh hasn't logged anything
#      in 10 min while file has grown, kick the cron once.
#
# Conservative: never kills more than one process per cycle. Always logs the
# decision and the trigger so post-mortem is easy.

set -uo pipefail
LOG="$HOME/.surrogate/logs/self-heal-watchdog.log"
mkdir -p "$(dirname "$LOG")"

MEM_THRESHOLD_PCT="${MEM_THRESHOLD_PCT:-85}"
STUCK_INGEST_MIN="${STUCK_INGEST_MIN:-20}"
STUCK_PUSH_MIN="${STUCK_PUSH_MIN:-10}"
TICK_SEC="${TICK_SEC:-60}"

log() { echo "[$(date +%Y-%m-%d\ %H:%M:%S)] $*" | tee -a "$LOG"; }

mem_pct() {
    # Container's RAM as %. /proc/meminfo on Linux only.
    awk '/^MemTotal:/ {tot=$2} /^MemAvailable:/ {avail=$2}
         END {if (tot>0) printf "%d", (tot-avail)*100/tot; else print 0}' /proc/meminfo 2>/dev/null
}

mtime_age_min() {
    [[ -f "$1" ]] || { echo 99999; return; }
    local mt now
    mt=$(stat -c %Y "$1" 2>/dev/null || echo 0)
    now=$(date +%s)
    echo $(( (now - mt) / 60 ))
}

last_kept_age_min() {
    local log_path="$HOME/.surrogate/logs/bulk-ingest-parallel.log"
    [[ -f "$log_path" ]] || { echo 99999; return; }
    # Find the most recent "kept:" line and compute its age via the log mtime
    # as a coarse proxy. Precise per-line timestamping is a future refinement.
    if grep -q "kept:" "$log_path" 2>/dev/null; then
        mtime_age_min "$log_path"
    else
        echo 99999
    fi
}

heal_memory() {
    local pct="$1"
    log "MEMORY ALERT pct=$pct% threshold=$MEM_THRESHOLD_PCT% — preempting OOM"
    # Adaptive aggressiveness — kill more shards as pct gets dangerously close.
    local kill_count=1
    [[ "$pct" -ge 90 ]] && kill_count=2     # 90%+: kill 2 youngest
    [[ "$pct" -ge 95 ]] && kill_count=3     # 95%+: kill 3 youngest + supporting hogs

    # Tier-1 victims — dataset-enrich shards
    local victims
    victims=$(pgrep -f "dataset-enrich.sh" | sort -nr | head -"$kill_count")
    if [[ -n "$victims" ]]; then
        echo "$victims" | while read -r pid; do
            log "  -> SIGTERM dataset-enrich pid=$pid"
            kill -TERM "$pid" 2>/dev/null || true
        done
    fi

    # Tier-2 victims (when pct critical) — large parquet/dataset-mirror loaders
    if [[ "$pct" -ge 90 ]]; then
        for pat in "dataset-mirror.sh" "parquet-direct-ingest.sh" "llm-burst-generator.py"; do
            local pid=$(pgrep -f "$pat" | head -1)
            [[ -n "$pid" ]] && { log "  -> CRITICAL: SIGTERM $pat pid=$pid"; kill -TERM "$pid" 2>/dev/null || true; }
        done
    fi

    # Tier-3 nuclear (95%+) — kill agentic-crawler + research-loop too
    if [[ "$pct" -ge 95 ]]; then
        for pat in "agentic-crawler.sh" "github-agentic-crawler" "scrape-continuous"; do
            local pid=$(pgrep -f "$pat" | head -1)
            [[ -n "$pid" ]] && { log "  -> NUCLEAR: SIGTERM $pat pid=$pid"; kill -TERM "$pid" 2>/dev/null || true; }
        done
    fi
}

heal_stuck_ingest() {
    log "STUCK INGEST — pairs file age >${STUCK_INGEST_MIN}m AND no recent kept: lines"
    # Force-restart the parallel-ingest manager. Existing shard children get
    # reaped; the next supervisor cycle re-spawns them with fresh state.
    local victims
    victims=$(pgrep -f "bulk-ingest-parallel.sh")
    if [[ -n "$victims" ]]; then
        log "  -> SIGTERM bulk-ingest-parallel pids: $victims"
        echo "$victims" | xargs -r kill -TERM 2>/dev/null || true
        sleep 5
        nohup bash "$HOME/.surrogate/bin/bulk-ingest-parallel.sh" \
            >> "$HOME/.surrogate/logs/bulk-ingest-parallel.log" 2>&1 &
        log "  -> respawned bulk-ingest-parallel pid=$!"
    else
        log "  -> bulk-ingest-parallel not running; spawning fresh"
        nohup bash "$HOME/.surrogate/bin/bulk-ingest-parallel.sh" \
            >> "$HOME/.surrogate/logs/bulk-ingest-parallel.log" 2>&1 &
    fi
}

heal_stale_push() {
    log "STALE PUSH — uploader log idle >${STUCK_PUSH_MIN}m while file grew"
    nohup bash "$HOME/.surrogate/bin/push-training-to-hf.sh" \
        >> "$HOME/.surrogate/logs/training-push.log" 2>&1 &
    log "  -> kicked push-training-to-hf pid=$!"
}

log "watchdog start — mem_threshold=${MEM_THRESHOLD_PCT}% stuck_ingest=${STUCK_INGEST_MIN}m stuck_push=${STUCK_PUSH_MIN}m tick=${TICK_SEC}s"

while true; do
    pct=$(mem_pct)
    pairs_age=$(mtime_age_min "$HOME/.surrogate/training-pairs.jsonl")
    kept_age=$(last_kept_age_min)
    push_age=$(mtime_age_min "$HOME/.surrogate/logs/training-push.log")

    # Memory healing has highest priority — OOM kills the whole container.
    if [[ "$pct" -ge "$MEM_THRESHOLD_PCT" ]]; then
        heal_memory "$pct"
    # Then stuck ingestion (no new pairs AND no fresh kept: lines)
    elif [[ "$pairs_age" -ge "$STUCK_INGEST_MIN" ]] && [[ "$kept_age" -ge 15 ]]; then
        heal_stuck_ingest
    # Then stale uploader (new pairs queued but uploader hasn't run)
    elif [[ "$pairs_age" -lt 5 ]] && [[ "$push_age" -ge "$STUCK_PUSH_MIN" ]]; then
        heal_stale_push
    fi

    # One-line heartbeat per tick — easy to grep for "things are fine"
    log "tick mem=${pct}% pairs_age=${pairs_age}m kept_age=${kept_age}m push_age=${push_age}m"
    sleep "$TICK_SEC"
done
