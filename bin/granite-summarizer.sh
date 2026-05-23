#!/usr/bin/env bash
# Granite summarizes recent decisions into a digest markdown.
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID="$(date +%Y%m%d_%H%M)"
OUT="$SHARED/reviews/${RUN_ID}_granite_digest.md"
LOG="$HOME/.claude/logs/granite-summarizer.log"
mkdir -p "$(dirname "$OUT")" "$(dirname "$LOG")"

# Collect last 10 decisions (last 30 min, skip already-summarized)
RECENT=$(find "$SHARED/decisions" -name '*.md' -mmin -30 -type f 2>/dev/null | sort | tail -10)
[[ -z "$RECENT" ]] && { echo "[$(date +%H:%M)] no recent decisions" >> "$LOG"; exit 0; }

PROMPT=$(cat <<'HDR'
Summarize these recent autonomous-agent pipeline decisions for a daily digest. Be concise.

For each file give: 1 line "WHAT was decided/done" + 1 line "WHY/IMPACT". Skip filler.

At the end produce:
- PROJECT OVERVIEW: 1 sentence per project (Costinel/Vanguard/arkship/surrogate)
- 3 KEY INSIGHTS across all decisions
- TOP 3 RISKS flagged

INPUT DECISIONS:
HDR
)
CONTENT=""
for f in $RECENT; do
    CONTENT+="$(printf '\n--- %s ---\n' "$(basename $f)")"
    CONTENT+="$(head -40 "$f")"
done

echo "[$(date +%H:%M)] summarizing $(echo "$RECENT" | wc -l | tr -d ' ') files" >> "$LOG"

RESPONSE=$(printf '%s\n%s' "$PROMPT" "$CONTENT" | /opt/surrogate-1-harvest/bin/granite-bridge.sh --model granite4 --max-tokens 1500 2>>"$LOG")
RC=$?
[[ $RC -ne 0 || -z "$RESPONSE" ]] && { echo "[$(date +%H:%M)] FAIL" >> "$LOG"; exit $RC; }

{
    echo "# Granite Digest — $RUN_ID"
    echo "Files summarized: $(echo "$RECENT" | wc -l | tr -d ' ')"
    echo ""
    echo "$RESPONSE"
} > "$OUT"

echo "[$(date +%H:%M)] wrote $OUT ($(wc -c < "$OUT") bytes)" >> "$LOG"
echo "Granite digest: $OUT"
