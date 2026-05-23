#!/usr/bin/env bash
# Generic ceremony/role agent runner — one script, config-driven.
# Usage: agent-runner.sh <role-name>
# Config:  ~/.hermes/config/ceremony-agents.json → roles.<name>.{title,task,model,prompt}
# Output:  ~/.hermes/workspace/ceremonies/<role>/YYYY-MM-DD_HH-MM.md
# Routing:
#   - If role.model starts with "opus"|"sonnet" → claude-bridge.sh (Max plan)
#   - Else → ai-fallback.sh --task <task>  (free-first chain)
set -u

ROLE="${1:?usage: agent-runner.sh <role> — see ~/.hermes/config/ceremony-agents.json}"
CFG="$HOME/.hermes/config/ceremony-agents.json"
LOG="$HOME/.claude/logs/agent-$ROLE.log"
OUT_DIR="$HOME/.hermes/workspace/ceremonies/$ROLE"
mkdir -p "$(dirname "$LOG")" "$OUT_DIR"

[[ ! -f "$CFG" ]] && { echo "[agent-runner] config missing: $CFG" | tee -a "$LOG"; exit 1; }

# Extract role config (title, task, model) via python3 — tab-separated so titles with spaces work
IFS=$'\t' read -r TITLE TASK MODEL < <(python3 -c "
import json, sys
d = json.load(open('$CFG'))
r = d.get('roles', {}).get('$ROLE')
if not r: sys.exit('role not found: $ROLE')
print(r.get('title','?'), r.get('task','creative'), r.get('model','none'), sep='\t')
") || { echo "[agent-runner] role '$ROLE' not in config" | tee -a "$LOG"; exit 1; }

PROMPT=$(python3 -c "
import json
d = json.load(open('$CFG'))
print(d['roles']['$ROLE'].get('prompt',''))
")

[[ -z "$PROMPT" ]] && { echo "[agent-runner] empty prompt for $ROLE" | tee -a "$LOG"; exit 1; }

DATE=$(date +%Y-%m-%d_%H-%M)
OUT="$OUT_DIR/${DATE}.md"
START=$(date +%s)
echo "[$(date '+%H:%M:%S')] $ROLE start (task=$TASK, model=$MODEL)" >> "$LOG"

# Route to appropriate AI
case "$MODEL" in
    opus|opus-force)
        RESULT=$(echo "$PROMPT" | "/opt/surrogate-1-harvest/bin/claude-bridge.sh" --model opus --force --timeout 180 2>>"$LOG")
        ;;
    sonnet)
        # pain-research + agentic roles need longer for WebSearch+WebFetch chains (5 min)
        TIMEOUT_S=180
        case "$ROLE" in pain-research|design-thinking|market-analysis) TIMEOUT_S=420 ;; esac
        RESULT=$(echo "$PROMPT" | "/opt/surrogate-1-harvest/bin/claude-bridge.sh" --model sonnet --timeout "$TIMEOUT_S" 2>>"$LOG")
        ;;
    none|null|"")
        # Free-first: ai-fallback with task-aware routing
        RESULT=$(echo "$PROMPT" | "/opt/surrogate-1-harvest/bin/ai-fallback.sh" --task "$TASK" 2>>"$LOG")
        ;;
    *)
        # Custom model ID — pass through to claude-bridge
        RESULT=$(echo "$PROMPT" | "/opt/surrogate-1-harvest/bin/claude-bridge.sh" --model "$MODEL" --timeout 180 2>>"$LOG")
        ;;
esac

DUR=$(( $(date +%s) - START ))
if [[ -z "$RESULT" ]]; then
    echo "[$(date '+%H:%M:%S')] $ROLE FAILED after ${DUR}s" >> "$LOG"
    exit 1
fi

# Write output with frontmatter
cat > "$OUT" <<EOF
---
role: $ROLE
title: $TITLE
task: $TASK
ran_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
duration_s: $DUR
---

$RESULT
EOF

echo "[$(date '+%H:%M:%S')] $ROLE OK → $OUT (${DUR}s, $(wc -c < "$OUT") bytes)" >> "$LOG"
echo "$OUT"
