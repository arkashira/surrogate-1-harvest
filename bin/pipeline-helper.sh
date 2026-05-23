#!/usr/bin/env bash
# Pipeline helper: determines PROJECT rotation, emits context for role agent.
# Usage: pipeline-helper.sh <role>
#   role = b4|b1a1|pm|dev|qa|hr|sprint|retro
set -u
ROLE="${1:?role required}"
SHARED="$HOME/.hermes/workspace/swarm-shared"
AXENTX="/Users/Ashira/axentx"
STATE_FILE="$HOME/.hermes/state/pipeline-cursor.txt"
LOG="$HOME/.claude/logs/axentx-pipeline.log"

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$LOG")"

# Weighted rotation (revenue first)
ROTATION=(Costinel Vanguard arkship Costinel Vanguard arkship surrogate Costinel Vanguard arkship Costinel Vanguard arkship workio)
last=$(cat "$STATE_FILE" 2>/dev/null || echo -1)
# Only advance cursor on B4 (first role in pipeline); other roles use same project as last B4
if [[ "$ROLE" == "b4" ]]; then
    next=$(( (last + 1) % ${#ROTATION[@]} ))
    echo "$next" > "$STATE_FILE"
else
    next=${last:-0}
fi
PROJECT="${ROTATION[$next]}"
PROJECT_PATH="$AXENTX/$PROJECT"

TS=$(date +%Y%m%d_%H%M)
RUN_ID="${TS}_${PROJECT}_${ROLE}"

echo "[$(date '+%H:%M:%S')] $ROLE $PROJECT (slot $next)" | tee -a "$LOG"

# Emit context
cat <<CTX
RUN_ID: $RUN_ID
ROLE: $ROLE
PROJECT: $PROJECT
PROJECT_PATH: $PROJECT_PATH
SHARED: $SHARED
BACKLOG_FILE: $SHARED/backlog.jsonl
PRIORITY_FILE: $SHARED/priority.json
AGENT_QUALITY_FILE: $SHARED/agent-quality.jsonl
RECENT_BACKLOG: $(tail -5 "$SHARED/backlog.jsonl" 2>/dev/null | head -5)
CURRENT_PRIORITIES: $(cat "$SHARED/priority.json" 2>/dev/null | head -1)
CTX
