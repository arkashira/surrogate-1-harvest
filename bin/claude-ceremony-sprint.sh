#!/usr/bin/env bash
# Daily Sprint Planning — runs 01:30 via pipe-sprint-plan cron. Uses Opus (night window).
set -u
SHARED="/opt/surrogate-1-harvest/state/swarm-shared"
DATE=$(date +%Y%m%d)
OUT="$SHARED/sprint/${DATE}_plan.md"
mkdir -p "$(dirname "$OUT")"

# Gather inputs
BACKLOG=$(cat "$SHARED/backlog.jsonl" 2>/dev/null | tail -30)
LATEST_RETRO=$(ls -t "$SHARED/retro/"*_retro.md 2>/dev/null | head -1)
RETRO_CONTENT=""
[[ -n "$LATEST_RETRO" ]] && RETRO_CONTENT=$(head -60 "$LATEST_RETRO")
CURRENT_PRIORITY=$(cat "$SHARED/priority.json" 2>/dev/null)
RECENT_DECISIONS=$(ls -t "$SHARED/decisions/"*.md 2>/dev/null | head -10 | xargs -I {} sh -c 'echo "## $(basename {})"; head -20 {}' 2>/dev/null | head -200)

PROMPT=$(cat <<END
You are the PM/PO for axentx — a 5-project portfolio (Costinel, Vanguard, arkship = revenue priority; surrogate = support; workio = low priority).

Run the DAILY SPRINT PLANNING ceremony. Output a sprint plan in Markdown.

INPUTS:

=== Current Backlog (last 30) ===
$BACKLOG

=== Previous Retrospective ===
$RETRO_CONTENT

=== Current Priority Queue ===
$CURRENT_PRIORITY

=== Recent Decisions (last 10) ===
$RECENT_DECISIONS

=== YOUR OUTPUT (Markdown) ===

# Sprint Plan — $(date +%Y-%m-%d)

## Sprint Goal (1 sentence)

## Selected Items (8-12 items across projects)
For each: {id, project, title, size, assignee_role, rationale}. Revenue tier (Costinel/Vanguard/arkship) gets 4/14 slots each, surrogate 1, workio 0.

## Priority Queue (top 5 for immediate Dev role)
Rank by value/effort/risk/fit. Include full priority.json block at the end in a fenced JSON code block.

## Capacity Budget
Token budget per role × 96 ticks/day = day budget. Watch: we have free-tier rate limits.

## Ceremonies this sprint
Standup, Review, Retro — when.

## Risks + Mitigations

Be concise. Focus on moving real product work forward in revenue projects.
END
)

echo "[$(date +%H:%M)] sprint planning starting" >> "/opt/surrogate-1-harvest/logs/claude-bridge.log"
# Sprint planning = important task → Opus 4.7 (force-bypass night window if off-hours)
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model opus --force --timeout 300)
RC=$?

if [[ $RC -ne 0 || -z "$RESPONSE" ]]; then
    echo "sprint failed rc=$RC" >&2
    exit $RC
fi

echo "$RESPONSE" > "$OUT"

# Extract priority.json block from response and update priority.json
OUT_PATH="$OUT" PRIO_PATH="$SHARED/priority.json" DATE="$DATE" python3 <<'PY'
import re, json, os
out = os.environ['OUT_PATH']
pp  = os.environ['PRIO_PATH']
d   = os.environ['DATE']
text = open(out).read()
m = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', text)
if m:
    try:
        prio = json.loads(m.group(1))
        if 'priorities' in prio:
            with open(pp) as f: existing = json.load(f)
            existing['priorities'] = prio['priorities']
            existing['last_updated'] = '2026-04-22T01:30:00'
            existing['sprint_id'] = f'sprint_{d}'
            with open(pp, 'w') as f: json.dump(existing, f, indent=2)
            print(f"Updated priority.json with {len(prio['priorities'])} items")
    except Exception as e:
        print(f"parse error: {e}")
PY

echo "Sprint plan written: $OUT"
