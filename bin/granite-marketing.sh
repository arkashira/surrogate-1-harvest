#!/usr/bin/env bash
# Granite marketing — produces positioning/messaging for the rotation project
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID="$(date +%Y%m%d_%H%M)"
LOG="$HOME/.claude/logs/granite-marketing.log"
mkdir -p "$(dirname "$LOG")"

PROJECT=$(bash /opt/surrogate-1-harvest/bin/pipeline-helper.sh marketing 2>/dev/null | awk -F: '/^PROJECT:/ {gsub(/^ +| +$/,"",$2); print $2; exit}')
[[ -z "$PROJECT" ]] && PROJECT="Costinel"

PROJECT_PATH="$HOME/axentx/$PROJECT"
README=$(head -80 "$PROJECT_PATH/README.md" 2>/dev/null || echo "No README found")

PROMPT=$(cat <<END
You are a product marketing lead for axentx. Project: $PROJECT

Based on the project README + domain knowledge, produce:

## Elevator Pitch (2 sentences)

## Target ICP (Ideal Customer Profile)
- Company size, industry, role

## Value Props (top 3)
Measurable outcome, not feature

## Positioning vs Competitors (1 paragraph)
"Unlike X which Y, we Z because W"

## Landing Page Hero Copy
- Headline (max 10 words)
- Subhead (max 20 words)
- 3 bullets of benefits

## Objection Handlers (3 most likely objections + rebuttal)

README:
$README
END
)

OUT="$SHARED/decisions/${RUN_ID}_${PROJECT}_marketing.md"

echo "[$(date +%H:%M)] granite-marketing $PROJECT" >> "$LOG"
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/granite-bridge.sh --model qwen-coder --max-tokens 1200 2>>"$LOG")
[[ -z "$RESPONSE" ]] && exit 1

{
    echo "# Marketing Positioning — $PROJECT ($RUN_ID)"
    echo "$RESPONSE"
} > "$OUT"

echo "Marketing done: $OUT"
