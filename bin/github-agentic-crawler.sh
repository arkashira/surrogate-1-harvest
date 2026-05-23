#!/usr/bin/env bash
# Wrapper for github-agentic-crawler.py — runs continuously with auto-restart.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/github-agentic-crawler.log"
mkdir -p "$(dirname "$LOG")"

if [[ -z "${GITHUB_TOKEN_POOL:-}" ]]; then
    echo "[$(date +%H:%M:%S)] WARN: GITHUB_TOKEN_POOL empty — exiting" | tee -a "$LOG"
    exit 0
fi

POOL_SIZE=$(echo "$GITHUB_TOKEN_POOL" | tr ',' '\n' | wc -l | tr -d ' ')
echo "[$(date +%H:%M:%S)] github-agentic-crawler start (pool=$POOL_SIZE tokens, ~$((POOL_SIZE * 5000)) req/h)" | tee -a "$LOG"

# Run continuously; if Python crashes, sleep 30s and restart
while true; do
    python3 "$HOME/.surrogate/bin/github-agentic-crawler.py" 0 >> "$LOG" 2>&1
    rc=$?
    echo "[$(date +%H:%M:%S)] crawler exited rc=$rc — restart in 30s" | tee -a "$LOG"
    sleep 30
done
