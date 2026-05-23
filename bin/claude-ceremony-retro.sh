#!/usr/bin/env bash
# Daily Retrospective — runs 05:30 via pipe-retro cron. Uses Opus (night window).
set -u
SHARED="/opt/surrogate-1-harvest/state/swarm-shared"
DATE=$(date +%Y%m%d)
YESTERDAY=$(date -v-1d +%Y%m%d)
OUT="$SHARED/retro/${DATE}_retro.md"
mkdir -p "$(dirname "$OUT")"

# Gather inputs: yesterday's decisions, agent quality scores, reviews
YESTERDAY_DECISIONS_COUNT=$(ls "$SHARED/decisions/" 2>/dev/null | grep -c "^${YESTERDAY}" || echo 0)
AGENT_QUALITY=$(cat "$SHARED/agent-quality.jsonl" 2>/dev/null | tail -50)
REVIEWS=$(ls -t "$SHARED/reviews/"*.md 2>/dev/null | head -20 | xargs -I {} head -15 {} | head -200)
YESTERDAY_COMMITS=$(
  for p in Costinel Vanguard arkship surrogate workio; do
    DIR="$HOME/axentx/$p"
    [[ -d "$DIR/.git" ]] && echo "## $p" && git -C "$DIR" log --since="${YESTERDAY} 00:00" --until="${YESTERDAY} 23:59" --oneline 2>/dev/null | head -15
  done
)

PROMPT=$(cat <<END
You are the Retrospective Facilitator for axentx. Run the DAILY RETROSPECTIVE for $YESTERDAY.

INPUTS:

=== Decisions Written Yesterday (count: $YESTERDAY_DECISIONS_COUNT) ===
$REVIEWS

=== Agent Quality Scores ===
$AGENT_QUALITY

=== Previous-day Commits per Project ===
$YESTERDAY_COMMITS

=== OUTPUT (Markdown) ===

# Retrospective — $YESTERDAY

## What Shipped (real deliverables, not intentions)
List by project.

## Wins
3-5 concrete wins.

## Misses (with root cause, not just symptom)
Ex: Dev worker hallucinated file creation. Root cause: no post-commit verification.

## Agent/Model Scorecard
For each role that ran yesterday, score 1-5 + recommendation:
- role | model | runs | score | recommendation (swap/keep/tune)

## Actions for today
3-5 concrete, small, actionable.

## Key lessons (append-ready for ~/.claude/memory/lessons_learned.md)
1 paragraph, dense, no fluff.

Be brutally honest. Flag hallucination patterns. Revenue tier (Cost/Van/ark) gets priority analysis.
END
)

echo "[$(date +%H:%M)] retro starting" >> "/opt/surrogate-1-harvest/logs/claude-bridge.log"
# Retrospective = important task → Opus 4.7 explicitly (bypass night-gate if triggered outside 01-06)
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model opus --force --timeout 300)
RC=$?
[[ $RC -ne 0 || -z "$RESPONSE" ]] && { echo "retro failed rc=$RC" >&2; exit $RC; }

echo "$RESPONSE" > "$OUT"

# Extract Key lessons section and append to lessons_learned
OUT_PATH="$OUT" YDATE="$YESTERDAY" python3 <<'PY'
import re, os
out = os.environ['OUT_PATH']
yd  = os.environ['YDATE']
text = open(out).read()
m = re.search(r'## Key lessons[^\n]*\n(.+?)(?=\n##|\Z)', text, re.DOTALL)
if m:
    lesson = m.group(1).strip()
    ll_path = os.path.expanduser('~/.claude/memory/lessons_learned.md')
    with open(ll_path, 'a') as f:
        f.write(f"\n\n## {yd}: Retrospective (auto)\n{lesson}\n")
    print(f"Appended lesson to lessons_learned.md ({len(lesson)} chars)")
PY

echo "Retro written: $OUT"
