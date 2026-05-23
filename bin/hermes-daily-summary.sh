#!/usr/bin/env bash
# Daily Hermes health summary → Discord
# Runs once per day via launchd, posts a single embed with key metrics.
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/hermes-daily-summary.log"
mkdir -p "$(dirname "$LOG")"

# ── Collect metrics ──────────────────────────────────────────────────────────
TODAY=$(date +%Y-%m-%d)
YESTERDAY=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d 'yesterday' +%Y-%m-%d)

# 1. Tasks completed (24h)
TASKS_DONE=$(grep -c "done in" ~/.surrogate/logs/hermes-dev-*-daemon.log 2>/dev/null | awk -F: '{s+=$2} END{print s+0}')

# 2. Tasks failed (24h)
TASKS_FAIL=$(grep -c "failed after" ~/.surrogate/logs/hermes-dev-*-daemon.log 2>/dev/null | awk -F: '{s+=$2} END{print s+0}')

# 3. Scrape activity
SCRAPE_TOTAL=$(sqlite3 ~/.surrogate/state/scrape-ledger.db "SELECT COUNT(*) FROM scraped" 2>/dev/null || echo "?")
SCRAPE_24H=$(sqlite3 ~/.surrogate/state/scrape-ledger.db "SELECT COUNT(*) FROM scraped WHERE scraped_at > datetime('now','-24 hours')" 2>/dev/null || echo "?")

# 4. Training pairs
PAIRS=$(wc -l ~/axentx/surrogate/data/training-jsonl/*.jsonl 2>/dev/null | tail -1 | awk '{print $1}' || echo "?")

# 5. Index docs
DOCS=$(sqlite3 ~/.surrogate/index.db "SELECT COUNT(*) FROM docs" 2>/dev/null || echo "?")

# 6. Episodes (surrogate memory)
EPISODES=$(wc -l ~/.surrogate/state/surrogate-memory/episodes.jsonl 2>/dev/null | awk '{print $1}' || echo 0)

# 7. Daemons running
DAEMONS_UP=$(pgrep -f "dev-cloud-daemon\|qwen-coder-daemon\|priority-json-watcher\|hermes" 2>/dev/null | wc -l | tr -d ' ')

# 8. Redis queue depth (current)
REDIS_DEPTH=0
for q in cerebras groq github samba nvidia cloudflare qwen-local; do
    L=$(redis-cli -h 127.0.0.1 -p 6379 LLEN "hermes:work:coding:$q" 2>/dev/null)
    REDIS_DEPTH=$((REDIS_DEPTH + ${L:-0}))
done

# 9. Recent errors (last 100 log lines)
ERR_COUNT=$(tail -200 ~/.surrogate/logs/*.log 2>/dev/null | grep -cE "ERROR|CRITICAL|Fatal|429|500" 2>/dev/null || echo 0)

# ── Build digest body ────────────────────────────────────────────────────────
BODY="$(cat <<EOF
**Tasks (24h)** — done: ${TASKS_DONE} · failed: ${TASKS_FAIL}
**Scrape** — ledger: ${SCRAPE_TOTAL} · added 24h: ${SCRAPE_24H}
**Corpus** — training pairs: ${PAIRS} · index docs: ${DOCS}
**Memory** — surrogate episodes: ${EPISODES}
**System** — daemons up: ${DAEMONS_UP} · queue depth: ${REDIS_DEPTH}
**Errors (recent)** — ${ERR_COUNT} flagged in logs
EOF
)"

# Pick severity
LEVEL="info"
[[ $TASKS_FAIL -gt 0 ]] && LEVEL="warn"
[[ $ERR_COUNT -gt 100 ]] && LEVEL="warn"
[[ $TASKS_DONE -eq 0 && $TASKS_FAIL -eq 0 ]] && LEVEL="warn"  # nothing happened all day

echo "[$(date '+%H:%M:%S')] sending daily summary (${LEVEL}): done=$TASKS_DONE fail=$TASKS_FAIL scrape=$SCRAPE_24H" >> "$LOG"

"$HOME/.surrogate/bin/notify-discord.sh" "$LEVEL" "Hermes daily summary · $TODAY" "$BODY"
