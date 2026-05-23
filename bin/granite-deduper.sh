#!/usr/bin/env bash
# Granite finds duplicate/overlapping backlog items + proposes merges.
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID="$(date +%Y%m%d_%H%M)"
OUT="$SHARED/reviews/${RUN_ID}_granite_dups.md"
LOG="$HOME/.claude/logs/granite-deduper.log"
mkdir -p "$(dirname "$OUT")" "$(dirname "$LOG")"

# Full backlog
ITEMS=$(cat "$SHARED/backlog.jsonl" 2>/dev/null)
[[ -z "$ITEMS" ]] && { echo "[$(date +%H:%M)] empty backlog" >> "$LOG"; exit 0; }
COUNT=$(echo "$ITEMS" | wc -l | tr -d ' ')

PROMPT=$(cat <<'HDR'
You are a backlog deduplication analyzer. Given the backlog items below, find:

1. DUPLICATES — pairs describing the exact same feature
2. OVERLAPS — items that share significant functionality (propose merge)
3. CONFLICTS — items with contradictory intent

Output format (markdown):
## Duplicates
- `item A preview` ↔ `item B preview` → recommended: merge
## Overlaps
- A ~ B (shared aspect: ...) → merge or split?
## Conflicts
- A vs B (conflict: ...) → resolution: ...

Be strict — only flag genuine overlap, not tangential similarity.

BACKLOG:
HDR
)

echo "[$(date +%H:%M)] deduping $COUNT items" >> "$LOG"

RESPONSE=$(printf '%s\n%s' "$PROMPT" "$ITEMS" | /opt/surrogate-1-harvest/bin/granite-bridge.sh --model qwen-coder --max-tokens 1200 2>>"$LOG")
RC=$?
[[ $RC -ne 0 ]] && exit $RC

{
    echo "# Granite Deduper — $RUN_ID"
    echo "Backlog size: $COUNT items"
    echo ""
    echo "$RESPONSE"
} > "$OUT"

DUPS=$(echo "$RESPONSE" | grep -c "↔\|~ " 2>/dev/null || echo 0)
echo "[$(date +%H:%M)] found ~$DUPS pairs" >> "$LOG"
echo "Dedup: $OUT (~$DUPS pairs)"
