#!/usr/bin/env bash
# Claude reviews recent git commits in axentx projects + verifies dev claims vs reality.
# Runs every :30 via pipe-claude-dev-review cron.
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
LOG="$HOME/.claude/logs/claude-dev-review.log"
RUN_ID="$(date +%Y%m%d_%H%M)"
mkdir -p "$(dirname "$LOG")" "$SHARED/reviews"

# Gather: latest 3 dev decision files + actual git commits per project
DEV_CLAIMS=""
for f in $(find "$SHARED/decisions" -name "*_dev.md" -mmin -60 2>/dev/null | sort | tail -3); do
    DEV_CLAIMS+="--- $(basename $f) ---"$'\n'
    DEV_CLAIMS+=$(head -30 "$f")$'\n\n'
done
[[ -z "$DEV_CLAIMS" ]] && { echo "[$(date +%H:%M)] no recent dev claims" >> "$LOG"; exit 0; }

ACTUAL_COMMITS=""
for p in Costinel Vanguard arkship surrogate workio; do
    DIR="$HOME/axentx/$p"
    [[ -d "$DIR/.git" ]] && {
        RECENT=$(git -C "$DIR" log --since="60 min ago" --name-only --pretty=format:"%h %s" 2>/dev/null | head -40)
        [[ -n "$RECENT" ]] && ACTUAL_COMMITS+="## $p"$'\n'"$RECENT"$'\n\n'
    }
done

PROMPT_FILE=$(mktemp)
{
  echo "You are a code-review auditor for an autonomous AI dev pipeline."
  echo "Dev agents sometimes HALLUCINATE — they claim to create files that don't exist."
  echo ""
  echo "TASK: Compare each dev agent's CLAIM to actual git commits. Flag discrepancies."
  echo ""
  echo "=== DEV AGENT CLAIMS ==="
  echo "$DEV_CLAIMS"
  echo ""
  echo "=== ACTUAL GIT COMMITS (last 60 min) ==="
  echo "$ACTUAL_COMMITS"
  echo ""
  echo "OUTPUT (strict JSON, one line per dev claim):"
  echo "{\"claim_file\":\"<basename>\",\"claimed_files\":[\"...\"],\"actually_committed\":true|false,\"hallucinated_files\":[\"...\"],\"score\":1-5,\"verdict\":\"OK|HALLUCINATION|PARTIAL\"}"
  echo ""
  echo "Then final line: {\"summary\":\"<1 sentence>\",\"hallucination_count\":N,\"action\":\"ok|warn|rollback\"}"
} > "$PROMPT_FILE"

# Dev review = review category → Sonnet (verifies dev claims vs reality)
REVIEW=$(cat "$PROMPT_FILE" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model sonnet --timeout 180 2>>"$LOG")
RC=$?
rm -f "$PROMPT_FILE"

[[ $RC -ne 0 || -z "$REVIEW" ]] && { echo "[$(date +%H:%M)] dev-review failed rc=$RC" >> "$LOG"; exit $RC; }

OUT="$SHARED/reviews/${RUN_ID}_claude_dev_review.md"
{
    echo "# Claude Dev-Review — $RUN_ID"
    echo "$REVIEW"
} > "$OUT"

HC=$(echo "$REVIEW" | grep -o '"verdict":"HALLUCINATION"' | wc -l | tr -d ' ')
PC=$(echo "$REVIEW" | grep -o '"verdict":"PARTIAL"' | wc -l | tr -d ' ')

if [[ $HC -gt 0 ]]; then
    echo "[$(date +%H:%M)] ⚠ $HC hallucinations, $PC partial → $OUT" | tee -a "$LOG"
    # Append to quality log
    python3 -c "
import json, datetime
with open('$SHARED/agent-quality.jsonl','a') as f:
    f.write(json.dumps({
        'ts': datetime.datetime.now().isoformat(),
        'source': 'claude-dev-review',
        'run_id': '$RUN_ID',
        'hallucinations': $HC,
        'partial': $PC,
        'review_file': '$OUT',
        'action': 'hr-should-flag-dev-model',
    }) + '\n')
"
fi

echo "Dev review: $HC hallucinations, $PC partial verdicts"
