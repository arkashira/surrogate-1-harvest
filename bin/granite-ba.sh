#!/usr/bin/env bash
# Granite BA — converts raw backlog items into BRDs (detailed specs with user stories)
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID="$(date +%Y%m%d_%H%M)"
LOG="$HOME/.claude/logs/granite-ba.log"
mkdir -p "$(dirname "$LOG")"

# Pick 1 raw backlog item that doesn't have a spec yet
RAW_ITEM=$(python3 <<'PY'
import json, os, glob
backlog = '/Users/Ashira/.hermes/workspace/swarm-shared/backlog.jsonl'
existing_specs = set()
for f in glob.glob('/Users/Ashira/.hermes/workspace/swarm-shared/backlog/*_brd.md'):
    base = os.path.basename(f)
    existing_specs.add(base)
raw_items = []
if os.path.exists(backlog):
    with open(backlog) as fh:
        for line in fh:
            try:
                e = json.loads(line)
                if e.get('status') == 'raw' and e.get('project') and e.get('project') != 'meta':
                    raw_items.append(e)
            except: pass
if raw_items:
    # Pick the most recent raw item
    item = raw_items[-1]
    print(json.dumps(item))
PY
)
[[ -z "$RAW_ITEM" ]] && { echo "[$(date +%H:%M)] no raw items to spec" >> "$LOG"; exit 0; }

PROJECT=$(echo "$RAW_ITEM" | python3 -c "import json,sys;print(json.loads(sys.stdin.read()).get('project','?'))")
ITEM=$(echo "$RAW_ITEM" | python3 -c "import json,sys;print(json.loads(sys.stdin.read()).get('item','?'))")

PROMPT=$(cat <<END
You are a Business Analyst writing a BRD (Business Requirements Document) for this backlog item:

Project: $PROJECT
Item: $ITEM

Produce a concise BRD in Markdown with these sections:

## Executive Summary (2 sentences)
## Stakeholders
Primary user, beneficiary, approver
## User Stories (3-5)
Format: "As a X, I want Y, so that Z"
## Functional Requirements (5-8 bullets)
Numbered FR-1, FR-2, ...
## Non-functional Requirements
Performance, security, reliability, compliance
## Acceptance Criteria (Given/When/Then format, 3-5)
## Dependencies
Other systems/teams/data required
## Out of Scope
What we will NOT build in v1
## Success Metrics
3 measurable KPIs with target numbers
## Estimated Effort
S / M / L  + rationale in 1 sentence
END
)

OUT="$SHARED/backlog/${RUN_ID}_${PROJECT}_brd.md"

echo "[$(date +%H:%M)] granite-ba $PROJECT: $ITEM" >> "$LOG"
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/granite-bridge.sh --model qwen-coder --max-tokens 1800 2>>"$LOG")
[[ -z "$RESPONSE" ]] && exit 1

{
    echo "# BRD: $ITEM"
    echo "Project: $PROJECT  |  Run: $RUN_ID"
    echo ""
    echo "$RESPONSE"
} > "$OUT"

echo "BRD done: $OUT"
