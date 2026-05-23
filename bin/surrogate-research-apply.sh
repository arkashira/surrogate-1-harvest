#!/usr/bin/env bash
# Surrogate Research Apply Loop — picks next quick-win from research queue,
# uses surrogate-orchestrate.sh (architect → dev → qa → reviewer) to ship the feature.
# Runs every 30 min via LaunchAgent.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/surrogate-research-apply.log"
QUEUE="$HOME/.hermes/workspace/research/queue.txt"
APPLIED="$HOME/.hermes/workspace/research/applied.log"
mkdir -p "$(dirname "$QUEUE")" "$(dirname "$LOG")"
touch "$QUEUE" "$APPLIED"

# ── Resource guard ───────────────────────────────────────────────────────────
LOAD=$(uptime | awk -F'load averages:' '{print $2}' | awk '{print int($1)}')
[[ $LOAD -gt 8 ]] && { echo "[$(date +%H:%M:%S)] paused load=$LOAD" >> "$LOG"; exit 0; }

# ── Pick first non-applied line from queue ──────────────────────────────────
LINE=$(grep -v "^#" "$QUEUE" | head -1 || true)
if [[ -z "$LINE" ]]; then
    echo "[$(date +%H:%M:%S)] empty queue — nothing to apply" >> "$LOG"
    exit 0
fi

# Already applied? skip
if grep -qF "$LINE" "$APPLIED" 2>/dev/null; then
    # Remove duplicate from queue
    grep -vF "$LINE" "$QUEUE" > "$QUEUE.new" && mv "$QUEUE.new" "$QUEUE"
    echo "[$(date +%H:%M:%S)] dedup-skip: $LINE" >> "$LOG"
    exit 0
fi

echo "[$(date +%H:%M:%S)] applying: $LINE" >> "$LOG"

# ── Run orchestrate to apply the change ─────────────────────────────────────
TASK="Apply this research-discovered improvement to Surrogate CLI: $LINE
Use the orchestrate pipeline (architect → dev → qa → reviewer).
Test the change before declaring done. Auto-commit on APPROVE."

START=$(date +%s)
"$HOME/.local/bin/surrogate-orchestrate.sh" "$TASK" >> "$LOG" 2>&1
RC=$?
DUR=$(( $(date +%s) - START ))

# ── Mark as applied + remove from queue ─────────────────────────────────────
if [[ $RC -eq 0 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] APPLIED ($DUR s): $LINE" >> "$APPLIED"
    grep -vF "$LINE" "$QUEUE" > "$QUEUE.new" && mv "$QUEUE.new" "$QUEUE"
    "$HOME/.local/bin/notify-discord.sh" 2>/dev/null success "✨ Feature applied" "$LINE · ${DUR}s" || true
else
    # Move to back of queue (try later) — but don't loop forever; max 3 retries
    RETRIES=$(grep -c "^# retry $LINE" "$QUEUE" 2>/dev/null || echo 0)
    if [[ $RETRIES -ge 3 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] FAILED-3x: $LINE" >> "$APPLIED"
        grep -vF "$LINE" "$QUEUE" > "$QUEUE.new" && mv "$QUEUE.new" "$QUEUE"
    else
        echo "# retry $LINE" >> "$QUEUE"
    fi
    "$HOME/.local/bin/notify-discord.sh" 2>/dev/null warn "⚠ Apply failed (retry $RETRIES)" "$LINE · ${DUR}s · rc=$RC" || true
fi

echo "[$(date +%H:%M:%S)] done rc=$RC ${DUR}s" >> "$LOG"
