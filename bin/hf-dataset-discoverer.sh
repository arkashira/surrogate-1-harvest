#!/usr/bin/env bash
# Wrapper for hf-dataset-discoverer.py — auto-restart on crash.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a
LOG="$HOME/.surrogate/logs/hf-dataset-discoverer.log"
mkdir -p "$(dirname "$LOG")"

if [[ -z "${HF_TOKEN:-}${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
    echo "[$(date +%H:%M:%S)] WARN: no HF_TOKEN — discoverer will work but rate-limited" | tee -a "$LOG"
fi

while true; do
    python3 "$HOME/.surrogate/bin/hf-dataset-discoverer.py" >> "$LOG" 2>&1
    rc=$?
    echo "[$(date +%H:%M:%S)] discoverer exited rc=$rc — restart in 60s" | tee -a "$LOG"
    sleep 60
done
