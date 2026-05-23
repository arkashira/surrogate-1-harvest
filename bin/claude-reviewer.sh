#!/usr/bin/env bash
# Claude reviews latest pipeline outputs for: hallucinations, wrong claims, contamination.
# Runs every 15 min via pipe-claude-reviewer cron.
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
LOG="$HOME/.claude/logs/claude-reviewer.log"
RUN_ID="$(date +%Y%m%d_%H%M)"
mkdir -p "$(dirname "$LOG")" "$SHARED/reviews"

# Collect last 5 decisions (no review yet)
DECISIONS=$(find "$SHARED/decisions" -type f -mmin -20 -name '*.md' 2>/dev/null | sort | tail -5)
[[ -z "$DECISIONS" ]] && { echo "[$(date +%H:%M)] no recent decisions" >> "$LOG"; exit 0; }

# Build review prompt  
PROMPT_FILE=$(mktemp)
{
  echo "You are a quality reviewer for an autonomous AI agent pipeline. Review these recent pipeline outputs."
  echo ""
  echo "REVIEW CRITERIA:"
  echo "1. Does the output contain HALLUCINATED claims (errors that didn't happen, files that don't exist)?"
  echo "2. Is the output actually useful or just filler?"
  echo "3. Did the agent actually DO the work it claimed, or just describe intentions?"
  echo "4. Any red flags: circular reasoning, repeated old errors, propagated contamination?"
  echo ""
  echo "COMMON HALLUCINATION TO WATCH FOR: workers claiming 'Bash syntax error in axentx-dev-loop.sh' — THAT SCRIPT IS VALID. Flag any such claim."
  echo ""
  echo "OUTPUT FORMAT (strict JSON, one line per file):"
  echo "For each file, emit: {\"file\":\"<basename>\",\"score\":1-5,\"useful\":true|false,\"hallucinated\":true|false,\"flags\":[\"...\"]}"
  echo "After all files, emit one line: {\"summary\":\"<1 sentence>\",\"action\":\"ok|warn|purge\"}"
  echo ""
  echo "=== FILES TO REVIEW ==="
  for f in $DECISIONS; do
    echo ""
    echo "--- $(basename $f) ---"
    head -80 "$f"  # cap per-file size
  done
} > "$PROMPT_FILE"

# Reviewer + hallucination-check = Sonnet (rarely used, cheaper than Opus, still high quality)
REVIEW=$(cat "$PROMPT_FILE" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model sonnet --timeout 180 2>>"$LOG")
RC=$?
rm -f "$PROMPT_FILE"

if [[ $RC -ne 0 || -z "$REVIEW" ]]; then
    echo "[$(date +%H:%M)] review failed rc=$RC" >> "$LOG"
    exit $RC
fi

# Save review
OUT="$SHARED/reviews/${RUN_ID}_claude_review.md"
{
    echo "# Claude Review — $RUN_ID"
    echo "Reviewed: $(echo "$DECISIONS" | wc -l | tr -d ' ') files"
    echo "Model: $(date +%H) in 01-06 window ? opus : sonnet"
    echo ""
    echo "$REVIEW"
} > "$OUT"

# Parse JSON findings — flag hallucinations
HALLUCINATION_COUNT=$(echo "$REVIEW" | grep -o '"hallucinated":true' | wc -l | tr -d ' ')
if [[ $HALLUCINATION_COUNT -gt 0 ]]; then
    echo "[$(date +%H:%M)] ⚠ $HALLUCINATION_COUNT hallucinations flagged → $OUT" | tee -a "$LOG"
    # Add to backlog for HR to act on
    python3 -c "
import json, datetime, os
entry = {
    'ts': datetime.datetime.now().isoformat(),
    'source': 'claude-reviewer',
    'project': 'meta',
    'item': f'{$HALLUCINATION_COUNT} hallucinations flagged in cycle ${RUN_ID}',
    'signal': 'anti-hallucination',
    'size': 'S',
    'severity': 'warn',
    'review_file': '$OUT',
}
with open(os.path.expanduser('$SHARED/backlog.jsonl'), 'a') as fh:
    fh.write(json.dumps(entry) + '\n')
"
fi

echo "[$(date +%H:%M)] reviewed $(echo "$DECISIONS" | wc -l | tr -d ' ') files; ${HALLUCINATION_COUNT} hallucinations" >> "$LOG"
# Print summary (visible to Hermes)
echo "Claude review done: $(echo "$DECISIONS" | wc -l | tr -d ' ') files, $HALLUCINATION_COUNT hallucinations"
