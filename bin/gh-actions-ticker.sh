#!/usr/bin/env bash
# GitHub Actions burst dispatcher — runs on HF Space, NOT Mac.
#
# Fires workflow_dispatch on every configured runner repo every 60s,
# bypassing the GitHub Actions cron */5 minimum interval. Combined with
# 8-min runner timeout, this saturates the 20-concurrent-job free-tier cap
# on each account. Excess fires queue and cycle through as slots free.
#
# Required env (set as HF Space secrets):
#   GH_TOKEN_ARKASHIRA    — PAT for arkashira/surrogate-1-runner
#   GH_TOKEN_DEVOPS       — PAT for ashiradevops-alt/surrogate-1-runner
#
# Skips silently if either is unset (don't break boot just because the
# operator hasn't configured GH dispatch yet).

set -uo pipefail
LOG="$HOME/.surrogate/logs/gh-actions-ticker.log"
mkdir -p "$(dirname "$LOG")"

TICK_SEC="${GH_TICK_SEC:-600}"   # Throttled 60s→600s (audit 2026-04-29):
                                  # 60s burst piled 200 jobs vs free-tier 20-concurrent
                                  # cap, 17h queue depth, zero completions. 600s lets
                                  # the queue actually drain. Override via GH_TICK_SEC.

dispatch() {
    local repo="$1"
    local tok="$2"
    [[ -z "$tok" ]] && return 0
    local resp
    resp=$(curl -sS --max-time 10 -X POST \
        -H "Authorization: token $tok" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$repo/actions/workflows/ingest.yml/dispatches" \
        -d '{"ref":"main"}' 2>&1)
    # GitHub returns 204 No Content on success (empty body).
    if [[ -z "$resp" ]]; then
        echo "[$(date +%H:%M:%S)] ✅ $repo dispatched"
    else
        # Truncate noisy error bodies to one line for log readability.
        echo "[$(date +%H:%M:%S)] ⚠ $repo: ${resp:0:160}"
    fi
}

echo "[$(date +%H:%M:%S)] gh-actions-ticker start  tick=${TICK_SEC}s" | tee -a "$LOG"

while true; do
    {
        dispatch "arkashira/surrogate-1-runner"     "${GH_TOKEN_ARKASHIRA:-}"
        dispatch "ashiradevops-alt/surrogate-1-runner" "${GH_TOKEN_DEVOPS:-}"
    } >> "$LOG" 2>&1
    sleep "$TICK_SEC"
done
