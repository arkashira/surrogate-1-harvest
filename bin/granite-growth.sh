#!/usr/bin/env bash
# Granite growth — generates acquisition/retention/referral ideas
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID="$(date +%Y%m%d_%H%M)"
LOG="$HOME/.claude/logs/granite-growth.log"
mkdir -p "$(dirname "$LOG")"

PROJECT=$(bash /opt/surrogate-1-harvest/bin/pipeline-helper.sh growth 2>/dev/null | awk -F: '/^PROJECT:/ {gsub(/^ +| +$/,"",$2); print $2; exit}')
[[ -z "$PROJECT" ]] && PROJECT="Costinel"

# Recent backlog + priority for context
BACKLOG=$(tail -15 "$SHARED/backlog.jsonl" 2>/dev/null)
PRIORITY=$(cat "$SHARED/priority.json" 2>/dev/null)

PROMPT=$(cat <<END
You are a growth strategist for axentx. Project: $PROJECT

Produce 6 concrete growth experiments:

## Acquisition Loop (2 ideas)
Low-cost channels suited for DevOps/SecOps buyers.

## Activation Loop (2 ideas)
Turn signup into "aha moment" within 24h.

## Retention Loop (2 ideas)
Make users return weekly.

Each experiment: { hypothesis, metric, effort (S/M/L), timeframe (days), expected lift (%) }

## Top Pick
Which single experiment should $PROJECT run first and why.

Recent backlog context:
$BACKLOG

Priority queue:
$PRIORITY
END
)

OUT="$SHARED/decisions/${RUN_ID}_${PROJECT}_growth.md"

echo "[$(date +%H:%M)] granite-growth $PROJECT" >> "$LOG"
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/granite-bridge.sh --model qwen-coder --max-tokens 1400 2>>"$LOG")
[[ -z "$RESPONSE" ]] && exit 1

{
    echo "# Growth Experiments — $PROJECT ($RUN_ID)"
    echo "$RESPONSE"
} > "$OUT"

# Append top pick to backlog as testable hypothesis
python3 <<PYEOF >> "$LOG" 2>&1
import json, re, datetime
with open('$OUT') as f: text = f.read()
m = re.search(r'## Top Pick\s*(.+?)(?=##|\Z)', text, re.DOTALL)
if m:
    pick = m.group(1).strip()[:200]
    with open('/Users/Ashira/.hermes/workspace/swarm-shared/backlog.jsonl','a') as f:
        f.write(json.dumps({
            'ts': datetime.datetime.now().isoformat(),
            'source': 'granite-growth',
            'project': '$PROJECT',
            'item': f'Growth experiment: {pick}',
            'signal': 'growth hacking',
            'size': 'S',
            'status': 'raw',
        }) + '\n')
    print('appended growth experiment to backlog')
PYEOF

echo "Growth done: $OUT"
