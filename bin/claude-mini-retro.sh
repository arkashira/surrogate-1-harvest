#!/usr/bin/env bash
# Quick retro on last 3 hours — fast adaptation. Sonnet for speed.
set -u
SHARED="/opt/surrogate-1-harvest/state/swarm-shared"
DATE=$(date +%Y%m%d_%H%M)
OUT="$SHARED/retro/mini_${DATE}.md"
LOG="/opt/surrogate-1-harvest/logs/claude-mini-retro.log"
mkdir -p "$(dirname "$OUT")" "$(dirname "$LOG")"

# Gather data from last 3 hours
RECENT_DECISIONS=$(find "$SHARED/decisions" -type f -mmin -180 -name "*.md" 2>/dev/null | sort | tail -15 | while read f; do
    echo "--- $(basename $f) ---"
    head -25 "$f"
done)

RECENT_REVIEWS=$(find "$SHARED/reviews" -type f -mmin -180 -name "*.md" 2>/dev/null | sort | tail -10 | while read f; do
    echo "--- $(basename $f) ---"
    head -20 "$f"
done)

# HR quality scorecard
AGENT_QUALITY=$(tail -30 "$SHARED/agent-quality.jsonl" 2>/dev/null)

# Latest sprint priorities (to compare against actual work)
CURRENT_PRIORITY=$(cat "$SHARED/priority.json" 2>/dev/null | head -40)

# Recent errors
RECENT_ERRORS=$(awk -v cutoff="$(date -v-3H '+%Y-%m-%d %H:%M')" '$1" "$2 > cutoff && /429|exhausted|FAIL|error/' ~/.hermes/logs/agent.log 2>/dev/null | tail -10 | head -5)

PROMPT="You are running a 3-HOUR MINI-RETRO — quick adjustment only, no deep analysis.

Review the last 3 hours of autonomous pipeline work and identify:
1. What unexpectedly succeeded (scale it up)
2. What silently failed (stop it)
3. Which agent needs immediate tuning (1 concrete change)
4. What should be added to priority.json NOW (1-2 items max)

Output format (strict Markdown, max 400 words):

## What's Working (bullets, 2-3)
## What's Broken (bullets + quick fix proposal)
## Immediate Actions (3 max, one-liner each)
## New Priority Items (JSON snippet for backlog append)

INPUTS:

### Recent decisions (last 3h)
$RECENT_DECISIONS

### Claude quality reviews (last 3h)
$RECENT_REVIEWS

### Agent quality scorecard
$AGENT_QUALITY

### Current priority queue
$CURRENT_PRIORITY

### Recent errors
$RECENT_ERRORS

Be brutally honest. Short, actionable."

echo "[$(date +%H:%M)] mini-retro start" >> "$LOG"
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model opus --force --timeout 120 2>>"$LOG")

if [[ -n "$RESPONSE" ]]; then
    {
        echo "# Mini Retro — $DATE"
        echo "_Window: last 3 hours | Model: Claude Sonnet_"
        echo ""
        echo "$RESPONSE"
    } > "$OUT"
    
    # Extract priority items suggestions and append to backlog
    python3 <<PY 2>>"$LOG"
import json, re, datetime
text = open("$OUT").read()
# Find JSON blocks with priority items
m = re.search(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
if not m:
    m = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
if m:
    try:
        data = json.loads(m.group(1))
        items = data if isinstance(data, list) else [data]
        backlog_path = "$SHARED/backlog.jsonl"
        added = 0
        with open(backlog_path, 'a') as f:
            for item in items[:3]:  # max 3 items per mini-retro
                entry = {
                    'ts': datetime.datetime.now().isoformat(),
                    'source': 'mini-retro',
                    'project': item.get('project','meta'),
                    'item': item.get('title',item.get('item','?'))[:200],
                    'signal': 'mini-retro adjustment',
                    'size': item.get('size','S'),
                    'status': 'raw',
                }
                f.write(json.dumps(entry) + '\n')
                added += 1
        print(f"  +{added} items to backlog")
    except Exception as e: print(f"  parse: {e}")
PY
    
    # Append compact lesson to lessons_learned
    python3 <<PY 2>>"$LOG"
import re
text = open("$OUT").read()
# Find Immediate Actions
m = re.search(r'## Immediate Actions\s*(.+?)(?=##|\Z)', text, re.DOTALL)
if m:
    actions = m.group(1).strip()[:500]
    with open("$HOME/.claude/memory/lessons_learned.md", 'a') as f:
        f.write(f"\n\n## $DATE: Mini-retro actions\n{actions}\n")
    print(f"  lessons updated")
PY
    
    echo "Mini-retro done: $OUT"
    echo "[$(date +%H:%M)] done" >> "$LOG"
else
    echo "[$(date +%H:%M)] FAIL (no response)" >> "$LOG"
    exit 1
fi
