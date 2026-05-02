#!/usr/bin/env bash
# axentx-codespace-keepalive.sh — keeps the ollama LLM-proxy codespace warm
# during business hours. Replaces the Mac-side LaunchAgent so the keepalive
# survives laptop sleep/shutdown.
#
# Account policy (2026-05-02):
#   ashirap         — FORBIDDEN (AI free APIs + git clone 5000/h only)
#   midnightcrisis  — quota exhausted this month
#   ashirapit       — primary codespace owner. Pinned via GH_TOKEN env.
#
# Strategy:
#   - During WORKING_HOURS_UTC (default 0:00–12:00 UTC, ≈ 7am–7pm Bangkok),
#     ping every PING_SEC to keep warm.
#   - Outside hours, no pings → codespace auto-stops at next 30 min idle.
#
# Required env:
#   GH_TOKEN                 ashirapit PAT (codespace + workflow scopes)
#   CS_NAME                  e.g. ollama-llm-proxy-r49955gvjxqv3ww4
#   CODESPACE_LLM_URL        e.g. https://<CS_NAME>-11434.app.github.dev
#
set -euo pipefail

CS_NAME="${CS_NAME:-ollama-llm-proxy-r49955gvjxqv3ww4}"
PING_SEC="${PING_SEC:-1200}"
WHS="${WHS:-0}"
WHE="${WHE:-12}"
# Default to journal-only (systemd captures stdout). Override LOG_FILE if you
# also want a file copy — must be in a path the service user can write to.
LOG_FILE="${LOG_FILE:-}"
if [ -n "$LOG_FILE" ]; then
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
fi

log() {
    if [ -n "$LOG_FILE" ]; then
        echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG_FILE"
    else
        echo "[$(date -u +%FT%TZ)] $*"
    fi
}

# `gh` reads GH_TOKEN env automatically — no `gh auth login` needed.
if [ -z "${GH_TOKEN:-}" ]; then
    log "FATAL: GH_TOKEN not set; cannot drive codespace API"
    exit 1
fi
export GH_TOKEN

log "start — keepalive for $CS_NAME (every ${PING_SEC}s, ${WHS}–${WHE} UTC)"

while true; do
    h=$(date -u +%H | sed 's/^0//')
    h=${h:-0}
    if [ "$h" -ge "$WHS" ] 2>/dev/null && [ "$h" -lt "$WHE" ] 2>/dev/null; then
        state=$(gh codespace view -c "$CS_NAME" --json state -q .state 2>&1 || echo "unknown")
        if [ "$state" != "Available" ]; then
            log "  state=$state — starting"
            gh codespace start -c "$CS_NAME" 2>&1 | tail -1 | xargs -I{} log "  start: {}"
            sleep 20
        fi
        if [ -n "${CODESPACE_LLM_URL:-}" ]; then
            r=$(curl -s -o /dev/null -w "%{http_code} %{time_total}s" -m 12 "$CODESPACE_LLM_URL/api/tags" 2>&1 || echo "fail")
            log "  ping → $r"
        else
            log "  CODESPACE_LLM_URL unset — skipping"
        fi
    else
        log "  outside hours (h=$h)"
    fi
    sleep "$PING_SEC"
done
