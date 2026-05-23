#!/usr/bin/env bash
# Continuous auto-orchestrate worker — replaces cron-fire-every-20min model.
#
# Spawns N parallel workers. Each loops forever:
#   pick TODO from random axentx repo → orchestrate pipeline → commit+push if APPROVE
#   → cool-down 5s → next iteration
#
# Avoids 'all hit same TODO' race via existing LOCK_DIR per-task hash.
# Resource guard: only pause if load > 80 (much higher tolerance vs old M%20 fire).
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/auto-orchestrate-continuous.log"
mkdir -p "$(dirname "$LOG")"

PARALLEL_WORKERS="${ORCHESTRATE_WORKERS:-4}"
WORKER_COOLDOWN="${WORKER_COOLDOWN:-10}"  # seconds between iterations per worker

echo "[$(date +%H:%M:%S)] continuous orchestrate start (workers=$PARALLEL_WORKERS, cooldown=${WORKER_COOLDOWN}s)" | tee -a "$LOG"

worker_loop() {
    local worker_id="$1"
    local iter=0
    while true; do
        iter=$((iter + 1))
        echo "[$(date +%H:%M:%S)] worker-$worker_id iter=$iter starting" >> "$LOG"

        # Resource guard — much more lenient than old M%20 cron
        local load
        load=$(uptime | sed -E 's/.*load average[s]?:[[:space:]]*//' | awk -F',' '{print int($1)}')
        load=${load:-0}
        if [[ $load -gt 80 ]]; then
            echo "[$(date +%H:%M:%S)] worker-$worker_id pause: load=$load > 80" >> "$LOG"
            sleep 60
            continue
        fi

        # Run single orchestrate cycle (existing script does TODO pick + run + push)
        bash "$HOME/.surrogate/bin/auto-orchestrate-loop.sh" >> "$LOG" 2>&1
        local rc=$?
        echo "[$(date +%H:%M:%S)] worker-$worker_id iter=$iter done rc=$rc" >> "$LOG"

        # Brief cooldown — workers stagger naturally
        sleep "$WORKER_COOLDOWN"
    done
}

# Spawn N workers in parallel
for i in $(seq 1 "$PARALLEL_WORKERS"); do
    worker_loop "$i" &
    sleep 3   # stagger startup
done
wait
