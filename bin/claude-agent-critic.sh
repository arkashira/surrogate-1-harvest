#!/usr/bin/env bash
# Agent-critic pattern (from Autogen/CrewAI) — pair recent outputs with adversarial review
# Picks last 3 decisions, sends to Claude acting as CRITIC, outputs improvement directive back to agents
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID=$(date +%Y%m%d_%H%M)
OUT="$SHARED/reviews/${RUN_ID}_agent_critic.md"
LOG="$HOME/.claude/logs/agent-critic.log"
mkdir -p "$(dirname "$OUT")" "$(dirname "$LOG")"

# Grab last 3 decisions that aren't already critiqued
LATEST=$(find "$SHARED/decisions" -type f -mmin -180 -name "*.md" 2>/dev/null | sort | tail -3)
[[ -z "$LATEST" ]] && { echo "no decisions to critique" >> "$LOG"; exit 0; }

CONTENT=""
for f in $LATEST; do
    CONTENT+="━━━ $(basename $f) ━━━"$'\n'
    CONTENT+="$(cat "$f")"$'\n\n'
done

PROMPT="You are the CRITIC agent in a pair-review system (inspired by Autogen GroupChat + CrewAI critic pattern).

Your job: adversarially review these 3 recent agent outputs. Find the SHARPEST criticism per doc.

For each: identify 1 MAJOR flaw + concrete fix.

Focus on:
- FACTUAL errors (hallucinated statistics, fake vendor names, wrong APIs)
- ARCHITECTURAL mistakes (wrong stack fit, violating project conventions)
- MISSING CONTEXT (didn't read existing code before writing)
- VAGUENESS (no specifics, generic templates)
- INCONSISTENCY (contradicts other recent decisions)

Output strict markdown (max 500 words):

## Critique 1: <filename>
**Flaw**: <1 specific issue>
**Evidence**: <quote from doc>
**Fix**: <concrete directive for next run>

## Critique 2: <filename>
...

## Cross-cutting pattern
<If multiple docs share a flaw, identify the pattern>

## Directive to agents
<1 sentence — what should ALL agents stop doing / start doing>

INPUT:
$CONTENT"

echo "[$(date +%H:%M)] agent-critic start" >> "$LOG"
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model opus --force --timeout 150 2>>"$LOG")
[[ -z "$RESPONSE" ]] && { echo "[$(date +%H:%M)] FAIL" >> "$LOG"; exit 1; }

{
    echo "# Agent Critic — $RUN_ID"
    echo "_Pattern: Autogen GroupChat + CrewAI critic. Sonnet-4.5_"
    echo ""
    echo "$RESPONSE"
} > "$OUT"

# Extract "Directive to agents" and append to a living AGENT_RULES.md that all crons read
python3 <<PY 2>>"$LOG"
import re, datetime
text = open("$OUT").read()
m = re.search(r'## Directive to agents\s*(.+?)(?=##|\Z)', text, re.DOTALL)
if m:
    directive = m.group(1).strip()[:300]
    rules_path = "$SHARED/AGENT_RULES.md"
    import os
    with open(rules_path, 'a') as f:
        f.write(f"\n## $RUN_ID: {directive}\n")
    print(f"  + directive added to AGENT_RULES.md")
PY

echo "Agent-critic done: $OUT"
