#!/usr/bin/env bash
# axentx secret-rotation watchdog.
#
# Reads data/secret-rotation.json and posts a Discord message via the
# webhook in $DISCORD_WEBHOOK when:
#   * any secret is overdue (next_due < today)
#   * any secret is due within `warn_days` (default 7)
#
# Designed for daily systemd timer (or `loop 24h`). Idempotent: if no
# secrets need attention, exits silently (no Discord noise).
#
# Env:
#   DISCORD_WEBHOOK            (required)
#   REPO_ROOT                  (default /opt/surrogate-1-harvest)
#   SECRET_ROTATION_FILE       (default $REPO_ROOT/data/secret-rotation.json)
#   SECRET_WATCHDOG_LOG        (default $REPO_ROOT/logs/secret-watchdog.log)
#   ALWAYS_POST=1              post a "all good" heartbeat even when nothing due
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/surrogate-1-harvest}"
ROTATION_FILE="${SECRET_ROTATION_FILE:-$REPO_ROOT/data/secret-rotation.json}"
LOG_FILE="${SECRET_WATCHDOG_LOG:-$REPO_ROOT/logs/secret-watchdog.log}"
WEBHOOK="${DISCORD_WEBHOOK:-}"
ALWAYS_POST="${ALWAYS_POST:-0}"

mkdir -p "$(dirname "$LOG_FILE")"
log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE"; }

if [[ ! -f "$ROTATION_FILE" ]]; then
  log "rotation file missing: $ROTATION_FILE"
  exit 0
fi
if [[ -z "$WEBHOOK" ]]; then
  log "DISCORD_WEBHOOK unset — would-have-posted only"
fi
if ! command -v jq >/dev/null 2>&1; then
  log "jq missing — cannot parse rotation file"
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  log "python3 missing — date math unavailable"
  exit 1
fi

WARN_DAYS=$(jq -r '.warn_days // 7' "$ROTATION_FILE")
TODAY=$(date -u +%Y-%m-%d)

# Walk every secret. Build two arrays: overdue + due-soon.
OVERDUE=()
DUE_SOON=()

while IFS=$'\t' read -r NAME NEXT_DUE OWNER ROTATE_URL; do
  [[ -z "$NAME" ]] && continue
  if [[ -z "$NEXT_DUE" || "$NEXT_DUE" == "null" ]]; then
    continue
  fi
  DELTA=$(python3 -c "
import datetime as dt
import sys
try:
    a = dt.date.fromisoformat('$TODAY')
    b = dt.date.fromisoformat('$NEXT_DUE')
    print((b - a).days)
except Exception as e:
    print('99999')
")
  if (( DELTA < 0 )); then
    OVERDUE+=("$(printf '• **%s** — overdue by %d days (was due %s) → %s' "$NAME" $((-DELTA)) "$NEXT_DUE" "$ROTATE_URL")")
  elif (( DELTA <= WARN_DAYS )); then
    DUE_SOON+=("$(printf '• **%s** — due in %d days (%s) → %s' "$NAME" "$DELTA" "$NEXT_DUE" "$ROTATE_URL")")
  fi
done < <(jq -r '.secrets[] | [.name, .next_due, .owner, .rotate_url] | @tsv' "$ROTATION_FILE")

n_overdue=${#OVERDUE[@]}
n_due_soon=${#DUE_SOON[@]}

if (( n_overdue == 0 && n_due_soon == 0 )); then
  log "all secrets within rotation window (warn_days=$WARN_DAYS) — quiet exit"
  if [[ "$ALWAYS_POST" != "1" ]]; then
    exit 0
  fi
  CONTENT=":white_check_mark: **Secret rotation watchdog** — all clean (warn=$WARN_DAYS days, $(jq '.secrets | length' "$ROTATION_FILE") secrets tracked)."
else
  CONTENT=":warning: **Secret rotation watchdog**"$'\n'
  if (( n_overdue > 0 )); then
    CONTENT+=$'\n**Overdue ('"$n_overdue"'):**\n'
    for line in "${OVERDUE[@]}"; do CONTENT+="$line"$'\n'; done
  fi
  if (( n_due_soon > 0 )); then
    CONTENT+=$'\n**Due within '"$WARN_DAYS"' days ('"$n_due_soon"'):**\n'
    for line in "${DUE_SOON[@]}"; do CONTENT+="$line"$'\n'; done
  fi
  CONTENT+=$'\nUpdate '"$ROTATION_FILE"' after rotating.'
fi

log "overdue=$n_overdue due_soon=$n_due_soon — posting"

if [[ -n "$WEBHOOK" ]]; then
  PAYLOAD=$(python3 -c '
import json, sys
print(json.dumps({"content": sys.stdin.read()}))
' <<< "$CONTENT")
  HTTP_CODE=$(curl -sS -o /tmp/.secret-watchdog.resp -w "%{http_code}" \
    -H "Content-Type: application/json" \
    -X POST "$WEBHOOK" \
    -d "$PAYLOAD" || echo "000")
  log "discord http=$HTTP_CODE"
  if [[ "$HTTP_CODE" != "204" && "$HTTP_CODE" != "200" ]]; then
    log "discord post failed: $(cat /tmp/.secret-watchdog.resp 2>/dev/null | head -c 500)"
    exit 1
  fi
else
  log "(no webhook — would post: $(echo "$CONTENT" | head -c 200)…)"
fi

exit 0
