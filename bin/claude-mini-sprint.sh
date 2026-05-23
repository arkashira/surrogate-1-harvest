#!/usr/bin/env bash
# Quick re-prioritization every 2 hours. Fast, focused on what's shippable next.
set -u
SHARED="/opt/surrogate-1-harvest/state/swarm-shared"
DATE=$(date +%Y%m%d_%H%M)
OUT="$SHARED/sprint/mini_${DATE}.md"
LOG="/opt/surrogate-1-harvest/logs/claude-mini-sprint.log"
mkdir -p "$(dirname "$OUT")" "$(dirname "$LOG")"

# Latest state
BACKLOG=$(tail -30 "$SHARED/backlog.jsonl" 2>/dev/null)
PRIORITY=$(cat "$SHARED/priority.json" 2>/dev/null)
RECENT_COMMITS=$(for p in Costinel Vanguard arkship surrogate; do
    DIR="$HOME/axentx/$p"
    [[ -d "$DIR/.git" ]] || continue
    R=$(git -C "$DIR" log --since="6 hours ago" --pretty="%h %s" 2>/dev/null | head -3)
    [[ -n "$R" ]] && echo "=== $p ===" && echo "$R"
done)
RECENT_DECISIONS=$(find "$SHARED/decisions" -type f -mmin -120 -name "*.md" 2>/dev/null | wc -l | tr -d ' ')

PROMPT="You are running a 2-HOUR MINI-SPRINT-PLAN. Re-prioritize based on latest momentum.

Current state:
- Backlog last 30 items:
$BACKLOG

- Current priority queue:
$PRIORITY

- Recent commits (last 6h):
$RECENT_COMMITS

- Decisions last 2h: $RECENT_DECISIONS

TASK: Output updated priority.json for next 2-hour window.

- Keep 3-5 top priorities
- Revenue-tier (Costinel/Vanguard/arkship) gets 80% weight
- If something shipped (in commits), mark status='implemented' and promote next item
- If hallucination pattern detected (reviewer flags), add 'RE-DO test-first' marker
- New high-signal items from backlog can jump queue if value > current top-3

Output STRICT format (only the JSON, no markdown fences):

{\"priorities\":[{\"id\":\"...\",\"project\":\"...\",\"title\":\"...\",\"score\":{\"value\":N,\"effort\":N,\"risk\":N,\"fit\":N,\"total\":N},\"assignee\":\"E1|E2|E10|...\",\"status\":\"ready|implemented|blocked|retest\"}],\"last_updated\":\"...\",\"sprint_id\":\"mini-$(date +%Y%m%d_%H)\",\"notes\":\"<1 sentence why this changed>\"}"

echo "[$(date +%H:%M)] mini-sprint start" >> "$LOG"
# Sprint planning = important task → Opus 4.7 (force-bypass night window)
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model opus --force --timeout 90 2>>"$LOG")

if [[ -n "$RESPONSE" ]]; then
    # Save markdown log
    {
        echo "# Mini Sprint Plan — $DATE"
        echo "_Window: 2-hour re-prioritization | Model: Claude Sonnet_"
        echo ""
        echo "$RESPONSE"
    } > "$OUT"
    
    # Extract and update priority.json if valid
    python3 <<PY 2>>"$LOG"
import json, re
text = """$RESPONSE"""
m = re.search(r'\{.*?"priorities"\s*:\s*\[.*?\].*?\}', text, re.DOTALL)
if m:
    try:
        data = json.loads(m.group(0))
        if 'priorities' in data and isinstance(data['priorities'], list):
            # Preserve structure with existing priority.json
            p_path = "$SHARED/priority.json"
            import os
            if os.path.exists(p_path):
                existing = json.load(open(p_path))
                existing['priorities'] = data['priorities']
                existing['last_updated'] = data.get('last_updated','')
                existing['sprint_id'] = data.get('sprint_id','')
                existing['mini_retro_notes'] = data.get('notes','')
                with open(p_path,'w') as f: json.dump(existing, f, indent=2)
                print(f"  updated priority.json with {len(data['priorities'])} items")
    except Exception as e: print(f"  parse: {e}")
PY
    echo "Mini-sprint done: $OUT"
    echo "[$(date +%H:%M)] done" >> "$LOG"
fi
