#!/usr/bin/env bash
# Granite business research — queries local RAG index for market/competitor signals per project
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID="$(date +%Y%m%d_%H%M)"
LOG="$HOME/.claude/logs/granite-business.log"
mkdir -p "$(dirname "$LOG")"

# Pick project from rotation
PROJECT=$(bash /opt/surrogate-1-harvest/bin/pipeline-helper.sh bd 2>/dev/null | awk -F: '/^PROJECT:/ {gsub(/^ +| +$/,"",$2); print $2; exit}')
[[ -z "$PROJECT" ]] && PROJECT="Costinel"

# Pull RAG context relevant to the project domain
DOMAIN_QUERY=""
case "$PROJECT" in
    Costinel)  DOMAIN_QUERY="cloud cost FinOps governance anomaly detection" ;;
    Vanguard)  DOMAIN_QUERY="cloud security posture management SOC2 compliance IAM" ;;
    arkship)   DOMAIN_QUERY="DevSecOps SRE supply chain SBOM k8s policy" ;;
    surrogate) DOMAIN_QUERY="LLM fine-tuning LoRA code model training" ;;
    *)         DOMAIN_QUERY="cloud platform DevOps" ;;
esac

RAG_CONTEXT=$(python3 /opt/surrogate-1-harvest/bin/ask-sqlite.py "$DOMAIN_QUERY 2026 market trend competitor" --max-docs 8 --no-llm 2>/dev/null | head -200)

PROMPT=$(cat <<END
You are a Business Research analyst for axentx. Project: $PROJECT

Domain: $DOMAIN_QUERY

Using the market/trend excerpts below (from our internal knowledge index), produce:

## Market Signals (3-5 bullets)
Key trends observed in the 2026 landscape

## Competitor Landscape
Name 3-5 competitors + their differentiators (if visible in excerpts)

## Positioning Opportunity
How $PROJECT can differentiate — 2-3 angles

## Feature Suggestions
3-5 features to add to backlog (one-liner each, with signal/rationale)

## Priority Score
Rank each suggested feature by: value (1-5) × feasibility (1-5)

RAG EXCERPTS:
$RAG_CONTEXT
END
)

OUT="$SHARED/decisions/${RUN_ID}_${PROJECT}_bd-research.md"

echo "[$(date +%H:%M)] granite-bd $PROJECT" >> "$LOG"
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/granite-bridge.sh --model qwen-coder --max-tokens 1500 2>>"$LOG")
[[ -z "$RESPONSE" ]] && { echo "[$(date +%H:%M)] FAIL bd" >> "$LOG"; exit 1; }

{
    echo "# Business Research — $PROJECT ($RUN_ID)"
    echo "Model: granite4:7b-a1b-h  |  Source: local RAG index"
    echo ""
    echo "$RESPONSE"
} > "$OUT"

# Extract feature suggestions and append to backlog
python3 <<PYEOF >> "$LOG" 2>&1
import json, re, datetime
with open('$OUT') as f: text = f.read()
# Find Feature Suggestions section
m = re.search(r'## Feature Suggestions(.*?)(?=##|\Z)', text, re.DOTALL)
if m:
    features = [line.strip('- *').strip() for line in m.group(1).split('\n') if line.strip().startswith(('-','*','1','2','3','4','5'))]
    backlog = '/Users/Ashira/.hermes/workspace/swarm-shared/backlog.jsonl'
    added = 0
    with open(backlog, 'a') as f:
        for feat in features[:5]:
            if len(feat) > 10 and len(feat) < 300:
                entry = {
                    'ts': datetime.datetime.now().isoformat(),
                    'source': 'granite-bd-research',
                    'project': '$PROJECT',
                    'item': feat[:200],
                    'signal': 'market trend analysis',
                    'size': 'M',
                    'status': 'raw',
                }
                f.write(json.dumps(entry) + '\n')
                added += 1
    print(f"appended {added} backlog items")
PYEOF

echo "BD research done: $OUT"
