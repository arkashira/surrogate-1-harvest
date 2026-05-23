#!/usr/bin/env bash
# Granite reads recent backlog items + tags them with project relevance + priority hint.
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID="$(date +%Y%m%d_%H%M)"
OUT="$SHARED/reviews/${RUN_ID}_granite_tags.md"
LOG="$HOME/.claude/logs/granite-tagger.log"
mkdir -p "$(dirname "$OUT")" "$(dirname "$LOG")"

# Last 20 backlog items that don't have tags yet
UNTAGGED=$(tail -20 "$SHARED/backlog.jsonl" 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        e = json.loads(line)
        if 'granite_tags' not in e:
            print(json.dumps(e))
    except: pass
")
[[ -z "$UNTAGGED" ]] && { echo "[$(date +%H:%M)] no untagged items" >> "$LOG"; exit 0; }

PROMPT=$(cat <<'HDR'
Tag each backlog item below. For each, output one JSON line:
{"item_preview":"<first 40 chars>","project_fit":["Costinel"|"Vanguard"|"arkship"|"surrogate"|"workio"], "priority_hint":"high|med|low","reusable_pattern":true|false,"cross_project":true|false}

Rules:
- Revenue-tier (Costinel/Vanguard/arkship) = high if cross-project
- surrogate/workio = low unless explicitly about training data
- reusable_pattern = true if solution works for multiple projects

INPUT:
HDR
)

echo "[$(date +%H:%M)] tagging $(echo "$UNTAGGED" | wc -l | tr -d ' ') items" >> "$LOG"

RESPONSE=$(printf '%s\n%s' "$PROMPT" "$UNTAGGED" | /opt/surrogate-1-harvest/bin/granite-bridge.sh --model granite4 --max-tokens 1200 2>>"$LOG")
RC=$?
[[ $RC -ne 0 ]] && exit $RC

{
    echo "# Granite Tagger — $RUN_ID"
    echo "$RESPONSE"
} > "$OUT"

# Append tagged items back to backlog (enriched)  — skip for now since it's derived output
echo "[$(date +%H:%M)] wrote $OUT" >> "$LOG"
echo "Tags: $OUT"
