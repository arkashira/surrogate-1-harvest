#!/usr/bin/env bash
# Autonomous dev loop — weighted priority (revenue-first).
# Costinel/Vanguard/arkship = high priority (2x frequency)
# surrogate = support (1x)
# workio = last (1x, only every 7th cycle)
set -e

# Weighted rotation — each slot = one cron cycle
# Pattern cycles every 7 steps:
# Costinel, Vanguard, arkship, Costinel, Vanguard, arkship, surrogate
# workio gets 1 slot every 14 cycles (every other full rotation)
ROTATION=(
    "Costinel"
    "Vanguard"
    "arkship"
    "Costinel"
    "Vanguard"
    "arkship"
    "surrogate"
    "Costinel"
    "Vanguard"
    "arkship"
    "Costinel"
    "Vanguard"
    "arkship"
    "workio"
)
AXENTX="/opt/axentx"
STATE_FILE="/opt/surrogate-1-harvest/state/axentx-dev-cursor.txt"
LOG="/opt/surrogate-1-harvest/logs/axentx-dev-loop.log"
SHARED="/opt/surrogate-1-harvest/state/swarm-shared"

mkdir -p "$(dirname "$STATE_FILE")"
mkdir -p "$(dirname "$LOG")"
mkdir -p "$SHARED"

last=$(cat "$STATE_FILE" 2>/dev/null || echo "-1")
next=$(( (last + 1) % ${#ROTATION[@]} ))
echo "$next" > "$STATE_FILE"

PROJECT="${ROTATION[$next]}"
PROJECT_PATH="$AXENTX/$PROJECT"

HOUR=$(date +%H)
FOCUSES=(discovery design backend frontend quality ops)
FOCUS_IDX=$(( (10#$HOUR + next) % 6 ))
FOCUS="${FOCUSES[$FOCUS_IDX]}"

CYCLE_ID="$(date +%Y%m%d_%H%M)_${PROJECT}_${FOCUS}"

echo "[$(date '+%Y-%m-%d %H:%M')] CYCLE=$CYCLE_ID PROJECT=$PROJECT FOCUS=$FOCUS (slot $next/14)" | tee -a "$LOG"

if [[ -x /opt/surrogate-1-harvest/bin/graph-sync.sh ]]; then
    /opt/surrogate-1-harvest/bin/graph-sync.sh >> "$LOG.graph" 2>&1 &
fi

cat <<EOF
CYCLE_ID: $CYCLE_ID
PROJECT: $PROJECT
PATH: $PROJECT_PATH
FOCUS: $FOCUS
SHARED_STATE: $SHARED
DESIGN_SPECS: $SHARED/designs
DECISIONS: $SHARED/decisions
METRICS: $SHARED/metrics
PAST_CYCLES: $SHARED/past-cycles
AGENT_ROSTER: $HOME/.hermes/workspace/agent-roster.json
LESSONS: $HOME/.claude/memory/lessons_learned.md
KNOWLEDGE: $HOME/.claude/memory/knowledge_index.md
AGENTS_MD: $PROJECT_PATH/AGENTS.md
PRIORITY_NOTE: Revenue-first rotation. Costinel/Vanguard/arkship = 4 slots each. surrogate = 1 (support). workio = 1 (last, low priority).
EOF
