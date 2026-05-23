#!/usr/bin/env bash
# Mythos-level audit with Opus 4.7 — domain expert review of recent work
# Usage: claude-mythos-audit.sh <coding|ops|ai-engineering|cloud|ai-agent>
set -u
DOMAIN="${1:?domain required}"
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID=$(date +%Y%m%d_%H%M)
OUT="$SHARED/decisions/${RUN_ID}_mythos-${DOMAIN}.md"
LOG="$HOME/.claude/logs/mythos-audit.log"
mkdir -p "$(dirname "$OUT")" "$(dirname "$LOG")"

# Personas per domain (Claude-Mythos-level expertise)
case "$DOMAIN" in
  coding)
    PERSONA="You are a PRINCIPAL ENGINEER with Mythos-level coding mastery. Deep knowledge of:
- Design patterns (GoF, DDD, hexagonal, clean architecture)
- Language idioms (Python/TS/Go/Rust/Java best practices)
- Refactoring (Fowler's catalog, code smells, technical debt)
- Algorithm complexity, data structures, system design
- Code review craft (readability, testability, maintainability)"
    SCOPE="Review recent code changes for: bad patterns, missing tests, over-engineering, wrong abstractions, performance risks, maintainability debt"
    ;;
  ops)
    PERSONA="You are a STAFF SRE with Mythos-level ops mastery. Deep knowledge of:
- SRE principles (SLIs/SLOs/error budgets, toil reduction)
- Production patterns (circuit breakers, bulkheads, backpressure)
- Incident response (runbooks, postmortems, blameless culture)
- Observability (RED/USE metrics, distributed tracing, structured logging)
- Release engineering (canary, blue-green, progressive delivery)
- Chaos engineering, capacity planning, auto-scaling"
    SCOPE="Review recent ops/infra changes for: reliability risks, missing observability, deployment flaws, scaling problems, runbook gaps"
    ;;
  ai-engineering)
    PERSONA="You are a PRINCIPAL AI ENGINEER with Mythos-level LLM mastery. Deep knowledge of:
- Prompt engineering (chain-of-thought, few-shot, system design)
- RAG architecture (chunking, embedding, reranking, hybrid search)
- Agent design (ReAct, Reflexion, tree-of-thoughts, planner-executor)
- Fine-tuning (PEFT, LoRA, QLoRA, DPO, instruction tuning)
- Evaluation (ragas, promptfoo, LLM-as-judge, offline + online evals)
- Serving (vLLM, SGLang, batching, quantization, routing)
- Production LLM (guardrails, cost management, latency budgets)"
    SCOPE="Review recent AI work for: prompt quality, RAG effectiveness, agent design flaws, eval gaps, cost/latency issues"
    ;;
  cloud)
    PERSONA="You are a PRINCIPAL CLOUD ARCHITECT with Mythos-level cloud mastery. Deep knowledge of:
- AWS Well-Architected (6 pillars), GCP Architecture Framework, Azure CAF
- IaC patterns (Terraform modules, Pulumi, CDK, Crossplane)
- Multi-cloud + hybrid patterns, avoiding vendor lock-in
- Cost optimization (RI, savings plans, spot, right-sizing, FinOps)
- Networking (VPC design, transit gateway, service mesh, zero-trust)
- Data architecture (data lakes, warehouses, streaming, ETL/ELT)
- Serverless patterns (event-driven, saga, choreography)"
    SCOPE="Review recent cloud/infra decisions for: architecture fit, cost waste, security gaps, networking flaws, operational burden"
    ;;
  ai-agent)
    PERSONA="You are a PRINCIPAL AGENT ENGINEER with Mythos-level agent mastery. Deep knowledge of:
- Multi-agent orchestration (GroupChat, Crew, StateGraph)
- Tool-use patterns (function calling, MCP protocol, tool routing)
- Memory hierarchies (working memory, episodic, semantic, reflection)
- Planning (ReAct, HTN, LLM-planner, subtask decomposition)
- Safety (guardrails, sandboxing, human-in-loop, reversibility)
- Self-improvement (reflection, skill synthesis, curriculum learning)"
    SCOPE="Review recent agent design/config for: orchestration flaws, tool misuse, memory leaks, planning gaps, safety issues"
    ;;
  *) echo "unknown domain: $DOMAIN"; exit 1 ;;
esac

# Recent work — code + decisions + commits
RECENT_WORK=""
case "$DOMAIN" in
  coding|cloud)
    for p in Costinel Vanguard arkship surrogate; do
        DIR="$HOME/axentx/$p"
        [[ -d "$DIR/.git" ]] || continue
        cd "$DIR"
        FILES=$(git log --since="6 hours ago" --name-only --pretty=format: 2>/dev/null | sort -u | grep -E '\.(py|ts|tsx|go|rs|tf|yaml|yml)$' | head -5)
        for f in $FILES; do
            [[ -f "$DIR/$f" ]] && {
                RECENT_WORK+="=== $p/$f ==="$'\n'
                RECENT_WORK+="$(head -180 "$DIR/$f")"$'\n\n'
            }
        done
    done
    ;;
  ops|ai-engineering|ai-agent)
    # Look at decisions + sandbox
    RECENT_WORK=$(find "$SHARED/decisions" -type f -mmin -360 -name "*.md" 2>/dev/null | sort | tail -10 | while read f; do
        echo "=== $(basename $f) ==="
        head -40 "$f"
    done)
    ;;
esac

[[ -z "$RECENT_WORK" ]] && { echo "no recent work for $DOMAIN" >> "$LOG"; exit 0; }

# Pull mythos-tagged context from RAG
MYTHOS_CTX=$(python3 /opt/surrogate-1-harvest/bin/ask-sqlite.py "$DOMAIN best practices patterns mastery" --source "mythos-$DOMAIN" 2>/dev/null | head -30)

PROMPT="$PERSONA

$SCOPE

Current axentx work to review:
$RECENT_WORK

Mythos-level context (curated master sources):
$MYTHOS_CTX

OUTPUT (strict Markdown, max 1200 words):

# Mythos Audit: $DOMAIN — $RUN_ID

## What's BELOW mythos level
For each issue: severity, evidence, specific fix reference (cite master source if available)

## What's AT mythos level
Praise specific patterns done right

## Directives (max 5)
Concrete changes to apply to next agent runs

## New skills to synthesize (JSON)
If any pattern is reusable: emit skill specs
[{\"skill_name\":\"<name>\",\"description\":\"<1 line>\",\"rationale\":\"<why>\"}]

Be ruthlessly honest. Cite specific anti-patterns by name. Reference master sources (Fowler refactoring, Google SRE book, OWASP cheat sheets, Anthropic cookbook, etc.)"

echo "[$(date +%H:%M)] mythos-audit $DOMAIN with Opus 4.7" >> "$LOG"
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model opus --force --timeout 300 2>>"$LOG")
[[ -z "$RESPONSE" ]] && { echo "FAIL" >> "$LOG"; exit 1; }

{
    echo "$RESPONSE"
    echo ""
    echo "_Model: Opus 4.7 | Domain: $DOMAIN | Corpus source: mythos-${DOMAIN}"
} > "$OUT"

# Extract skills + directives → apply
python3 <<PY 2>>"$LOG"
import re, json, os, datetime
text = open("$OUT").read()
# Extract skill JSON
m = re.search(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
if m:
    try:
        skills = json.loads(m.group(1))
        skill_dir = os.path.expanduser(f"~/.hermes/skills/mythos-$DOMAIN")
        os.makedirs(skill_dir, exist_ok=True)
        for s in skills[:3]:
            name = s.get('skill_name','').lower().replace(' ','-')
            if not name: continue
            if os.path.exists(f"{skill_dir}/{name}/SKILL.md"): continue
            os.makedirs(f"{skill_dir}/{name}", exist_ok=True)
            with open(f"{skill_dir}/{name}/SKILL.md", 'w') as f:
                f.write(f'''---
name: {name}
description: {s.get('description','')}
version: 1.0.0
author: MythosAudit-$DOMAIN
domain: $DOMAIN
tags: [mythos, $DOMAIN]
created_at: {datetime.datetime.now().isoformat()}
---

# {name.replace('-',' ').title()}

## Rationale
{s.get('rationale','')}

## Source
Mythos audit run: $RUN_ID
''')
            print(f"  + skill: mythos-$DOMAIN/{name}")
    except Exception as e: print(f"  skill parse: {e}")

# Extract directives and append to AGENT_RULES
m2 = re.search(r'## Directives[^\n]*\n(.+?)(?=##|\Z)', text, re.DOTALL)
if m2:
    directives = m2.group(1).strip()[:800]
    rules_path = os.path.expanduser("~/.hermes/workspace/swarm-shared/AGENT_RULES.md")
    with open(rules_path, 'a') as f:
        f.write(f"\n## [$RUN_ID] Mythos-$DOMAIN directives:\n{directives}\n")
    print(f"  + directives → AGENT_RULES.md")
PY

echo "Mythos audit $DOMAIN: $OUT"
