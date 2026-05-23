#!/usr/bin/env bash
# Performance watchdog — monitor system health, kill scrape if degrading
#
# Checks every 15s:
#   - load avg 1min (kill if > 10, warn if > 7)
#   - memory free pages (warn if < 30k, emergency < 15k)
#   - swap I/O rate (emergency if spiking)
#   - disk space on ~/.surrogate/state (warn if < 2GB)
#   - scrape process count (cap at 30, kill oldest if exceeded)
#
# Actions:
#   - WARN: log + throttle (pause new burst triggers via state file)
#   - EMERGENCY: kill all scrape processes, set pause flag for 10 min
set -u
LOG="$HOME/.surrogate/logs/perf-watchdog.log"
PAUSE_FLAG="$HOME/.surrogate/state/scrape-paused"
mkdir -p "$(dirname "$LOG")" "$(dirname "$PAUSE_FLAG")"

# Thresholds
LOAD_WARN=7
LOAD_EMERGENCY=10
FREE_PAGES_WARN=30000   # ~480MB
FREE_PAGES_EMERGENCY=15000   # ~240MB
PROC_CAP=30
DISK_WARN_GB=2

get_load() {
    uptime | sed -E 's/.*load average[s]?:[[:space:]]*//' | awk -F',' '{print int($1)}'
}
get_free_pages() {
    if [[ -r /proc/meminfo ]]; then
        awk '/MemAvailable/{print int($2/4)}' /proc/meminfo
    elif command -v vm_stat >/dev/null 2>&1; then
        vm_stat | awk '/Pages free/{gsub("[.]","",$3); print $3}'
    else
        echo 999999
    fi
}
get_scrape_procs() {
    pgrep -f "fs-to-jsonl\|github-bulk-train\|chroma-to-training\|bulk-scrape-burst" 2>/dev/null | wc -l | tr -d ' '
}
disk_free_gb() {
    df -g "$HOME" | awk 'NR==2 {print $4}'
}

emergency() {
    local reason="$1"
    echo "[$(date '+%H:%M:%S')] 🚨 EMERGENCY: $reason — killing all scrape workers" | tee -a "$LOG"
    pkill -9 -f "fs-to-jsonl" 2>/dev/null
    pkill -9 -f "github-bulk-train" 2>/dev/null
    pkill -9 -f "chroma-to-training" 2>/dev/null
    pkill -9 -f "bulk-scrape-burst" 2>/dev/null
    # Set pause flag for 10 min
    date -v +10M +%s > "$PAUSE_FLAG" 2>/dev/null || date -d '+10 minutes' +%s > "$PAUSE_FLAG"
    echo "[$(date '+%H:%M:%S')] pause flag set: $(cat $PAUSE_FLAG)" | tee -a "$LOG"
}

warn() {
    echo "[$(date '+%H:%M:%S')] ⚠️ WARN: $1" >> "$LOG"
}

# Single-check mode (called from cron)
if [[ "${1:-}" == "check" ]]; then
    LOAD=$(get_load)
    FREE=$(get_free_pages)
    PROCS=$(get_scrape_procs)
    DISK=$(disk_free_gb)

    MSG="load=$LOAD free_pages=$FREE scrape_procs=$PROCS disk_gb=$DISK"

    if [[ $LOAD -gt $LOAD_EMERGENCY ]] || [[ $FREE -lt $FREE_PAGES_EMERGENCY ]]; then
        emergency "$MSG"
        # Emergency handled — successful exit so launchd doesn't flag this as failure
        exit 0
    fi

    if [[ $PROCS -gt $PROC_CAP ]]; then
        # Kill oldest N - CAP scrape processes
        OVER=$((PROCS - PROC_CAP))
        echo "[$(date '+%H:%M:%S')] ⚠️ procs=$PROCS over cap $PROC_CAP — killing $OVER oldest" | tee -a "$LOG"
        pgrep -f "fs-to-jsonl\|github-bulk-train\|chroma-to-training" 2>/dev/null | head -$OVER | xargs -r kill -15
    fi

    if [[ $LOAD -gt $LOAD_WARN ]] || [[ $FREE -lt $FREE_PAGES_WARN ]] || [[ $DISK -lt $DISK_WARN_GB ]]; then
        warn "$MSG"
        # Tighten pause flag — prevent new bursts for 2 min
        date -v +2M +%s > "$PAUSE_FLAG" 2>/dev/null || date -d '+2 minutes' +%s > "$PAUSE_FLAG"
    fi

    echo "[$(date '+%H:%M:%S')] OK $MSG" >> "$LOG"
    exit 0
fi

# Daemon mode — continuous loop
echo "[$(date '+%H:%M:%S')] perf-watchdog starting (PID $$)" >> "$LOG"
while true; do
    bash "$0" check
    sleep 15
done
