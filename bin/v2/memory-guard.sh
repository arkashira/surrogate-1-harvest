#!/usr/bin/env bash
# Surrogate-1 v2 — memory-pressure guard for cron entries.
#
# Usage in cron line:
#   bash memory-guard.sh && bash heavy-task.sh
#   # ↑ heavy-task only runs if free memory >= MIN_FREE_MB
#
# Default threshold 3 GB (3072 MB). Adjust via $MIN_FREE_MB env.
# Returns 0 (proceed) if enough free memory; 1 (skip) if pressure.
set -uo pipefail

MIN_FREE_MB="${MIN_FREE_MB:-3072}"

# Linux container (HF Space) /proc/meminfo
if [[ -r /proc/meminfo ]]; then
    AVAIL=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
elif command -v vm_stat >/dev/null 2>&1; then
    # macOS fallback (used by anchor cron-loop running locally on dev)
    PAGES_FREE=$(vm_stat | awk '/Pages free/{gsub("\\.","",$3); print $3}')
    PAGE_SIZE=$(vm_stat | awk '/page size of/{print $8}')
    AVAIL=$(( PAGES_FREE * PAGE_SIZE / 1048576 ))
else
    # Unknown — assume OK
    AVAIL=99999
fi

if (( AVAIL >= MIN_FREE_MB )); then
    exit 0   # proceed
fi

# Pressure — log + skip
LOG="${MEMORY_GUARD_LOG:-${HOME}/.surrogate/logs/memory-guard.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
echo "[$(date '+%H:%M:%S')] SKIP — avail=${AVAIL}MB < ${MIN_FREE_MB}MB threshold" >> "$LOG" 2>/dev/null || true
exit 1
