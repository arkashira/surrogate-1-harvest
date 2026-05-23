#!/usr/bin/env bash
# Discord notifier — Hermes events → Discord webhook.
#
# Usage:
#   notify-discord.sh <level> <title> [body]
#   echo "body" | notify-discord.sh <level> <title>
#
# Levels: info | success | warn | error | scrape | task | budget
#
# Examples:
#   notify-discord.sh success "Task done" "p42 completed in 180s"
#   notify-discord.sh error "Daemon crashed" "qwen-coder exit 1"
#   tail -50 ~/.surrogate/logs/scrape.log | notify-discord.sh scrape "Scrape report"
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LEVEL="${1:-info}"
TITLE="${2:-Hermes notification}"
BODY="${3:-}"

# Read body from stdin if not provided as arg + stdin has data
if [[ -z "$BODY" ]] && [[ ! -t 0 ]]; then
    BODY="$(cat)"
fi

[[ -z "${DISCORD_WEBHOOK:-}" ]] && { echo "DISCORD_WEBHOOK not set in ~/.hermes/.env" >&2; exit 1; }

# Color + emoji per level
case "$LEVEL" in
    info)    COLOR=3447003;  EMOJI="ℹ️" ;;     # blue
    success) COLOR=3066993;  EMOJI="✅" ;;     # green
    warn)    COLOR=15844367; EMOJI="⚠️" ;;     # gold
    error)   COLOR=15158332; EMOJI="❌" ;;     # red
    scrape)  COLOR=10181046; EMOJI="🕷️" ;;    # purple
    task)    COLOR=1752220;  EMOJI="🛠️" ;;    # teal
    budget)  COLOR=15105570; EMOJI="💰" ;;     # orange
    *)       COLOR=9807270;  EMOJI="📌" ;;     # gray
esac

# Truncate body to Discord embed limit (4096) and escape JSON
BODY_TRUNC="${BODY:0:3900}"
BODY_JSON=$(python3 -c "
import json, sys
print(json.dumps(sys.argv[1]))
" "$BODY_TRUNC")

# Build payload
PAYLOAD=$(cat <<EOF
{
  "username": "Hermes",
  "avatar_url": "https://i.imgur.com/0nCrGLE.png",
  "embeds": [{
    "title": "${EMOJI} ${TITLE}",
    "description": ${BODY_JSON},
    "color": ${COLOR},
    "footer": {"text": "Surrogate-1 · $(hostname -s) · $(date '+%H:%M')"}
  }]
}
EOF
)

# Fire and forget (5s timeout, retry once on failure)
for attempt in 1 2; do
    HTTP=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$DISCORD_WEBHOOK" \
        -H "Content-Type: application/json" \
        --max-time 5 \
        -d "$PAYLOAD" 2>/dev/null)
    if [[ "$HTTP" =~ ^2 ]]; then
        exit 0
    fi
    sleep 1
done

echo "[discord] failed after 2 attempts (HTTP $HTTP)" >&2
exit 1
