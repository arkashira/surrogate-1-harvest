#!/usr/bin/env bash
# Dev orchestrator v2 — SANDBOX MODE
# Dev agents write to $PROJECT_PATH/.hermes-dev-sandbox/<priority_id>/ only.
# Real code integration is a separate manual/Claude step.
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
RUN_ID="$(date +%Y%m%d_%H%M)"
LOG="$HOME/.claude/logs/dev-orchestrator.log"
mkdir -p "$(dirname "$LOG")"

CTX=$(bash /opt/surrogate-1-harvest/bin/pipeline-helper.sh dev)
PROJECT=$(echo "$CTX" | awk -F: '/^PROJECT:/ {gsub(/^ +| +$/,"",$2); print $2; exit}')
PROJECT_PATH=$(echo "$CTX" | awk -F: '/^PROJECT_PATH:/ {gsub(/^ +| +$/,"",$2); print $2; exit}')
[[ -z "$PROJECT" ]] && { echo "no project"; exit 1; }
[[ -d "$PROJECT_PATH" ]] || { echo "path not found"; exit 1; }

[[], {}]
[[ -z "$PRIORITY" ]] && { echo "no ready priority for $PROJECT"; exit 0; }

PRIO_ID=$(echo "$PRIORITY" | python3 -c "import json,sys;print(json.loads(sys.stdin.read())['id'])")
PRIO_TITLE=$(echo "$PRIORITY" | python3 -c "import json,sys;print(json.loads(sys.stdin.read())['title'])")

# SANDBOX directory — isolated from real code
SANDBOX="$PROJECT_PATH/.hermes-dev-sandbox/${PRIO_ID}_${RUN_ID}"
mkdir -p "$SANDBOX/backend" "$SANDBOX/frontend" "$SANDBOX/docs"

echo "[$(date +%H:%M)] $PROJECT priority=$PRIO_ID sandbox=$SANDBOX" >> "$LOG"

# Prompts now target SANDBOX — not real paths. No risk of overwriting existing code.
E1_PROMPT="You are E1-Backend engineer. Implement: $PRIO_TITLE for project $PROJECT.

WRITE ALL FILES to $SANDBOX/backend/ (sandbox directory — safe area, nothing else exists there).

Output ONLY a shell script (no explanation, no markdown fences). Use cat > heredocs. Create <=3 files with real production-quality Python code. Include tests.

Begin with 'mkdir -p $SANDBOX/backend' and write files."

E2_PROMPT="You are E2-Frontend engineer. UI for: $PRIO_TITLE

WRITE ALL FILES to $SANDBOX/frontend/ (sandbox, empty).

Output ONLY a shell script. Create <=2 TSX components with real JSX. No markdown, no thinking."

E10_PROMPT="You are E10-AI engineer. Write a 30-line design doc for AI/ML enhancement of: $PRIO_TITLE

Markdown only. Sections: Technique, Data, Model, Integration, Effort. No code."

# Parallel execution — Groq fast for code, granite for design doc
(echo "$E1_PROMPT" | /opt/surrogate-1-harvest/bin/groq-bridge.sh --model fast --max-tokens 2500 > /tmp/dev-orch/e1.sh 2>/tmp/dev-orch/e1.err) &
E1_PID=$!
(echo "$E2_PROMPT" | /opt/surrogate-1-harvest/bin/groq-bridge.sh --model fast --max-tokens 1800 > /tmp/dev-orch/e2.sh 2>/tmp/dev-orch/e2.err) &
E2_PID=$!
(echo "$E10_PROMPT" | /opt/surrogate-1-harvest/bin/granite-bridge.sh --max-tokens 1200 > /tmp/dev-orch/e10.md 2>/tmp/dev-orch/e10.err) &
E10_PID=$!
mkdir -p /tmp/dev-orch

wait $E1_PID; E1_RC=$?
wait $E2_PID; E2_RC=$?
wait $E10_PID; E10_RC=$?

# Extract + safety check: ALL paths must start with $SANDBOX
extract_and_verify() {
    local file="$1"
    python3 <<PY
import re, sys, os
with open("$file") as f: text = f.read()
text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
m = re.search(r'\`\`\`(?:bash|sh|shell)?\n(.*?)\`\`\`', text, re.DOTALL)
script = m.group(1) if m else text

# SAFETY: every 'cat > PATH' must be inside sandbox
sandbox = "$SANDBOX"
unsafe = []
for m in re.finditer(r'cat\s+>+\s+(\S+)\s*<<', script):
    p = m.group(1).strip('"').strip("'")
    if not p.startswith(sandbox):
        unsafe.append(p)

if unsafe:
    print(f"UNSAFE", file=sys.stderr)
    for p in unsafe: print(f"  out of sandbox: {p}", file=sys.stderr)
    sys.exit(1)
if not re.search(r'^(cat >|mkdir -p|touch)', script, re.MULTILINE):
    sys.exit(2)
print(script)
PY
}

# Execute E1
if [[ $E1_RC -eq 0 ]]; then
    SAFE_SCRIPT=$(extract_and_verify /tmp/dev-orch/e1.sh 2>>"$LOG")
    if [[ -n "$SAFE_SCRIPT" ]]; then
        echo "[$(date +%H:%M)] executing E1 backend in sandbox" >> "$LOG"
        bash -c "$SAFE_SCRIPT" >>"$LOG" 2>&1 || echo "[$(date +%H:%M)] E1 partial failure (continued)" >> "$LOG"
    else
        echo "[$(date +%H:%M)] E1 rejected — out-of-sandbox writes" >> "$LOG"
    fi
fi

# Execute E2
if [[ $E2_RC -eq 0 ]]; then
    SAFE_SCRIPT=$(extract_and_verify /tmp/dev-orch/e2.sh 2>>"$LOG")
    if [[ -n "$SAFE_SCRIPT" ]]; then
        echo "[$(date +%H:%M)] executing E2 frontend in sandbox" >> "$LOG"
        bash -c "$SAFE_SCRIPT" >>"$LOG" 2>&1 || true
    else
        echo "[$(date +%H:%M)] E2 rejected — out-of-sandbox writes" >> "$LOG"
    fi
fi

# Save E10 design doc directly to sandbox
[[ $E10_RC -eq 0 ]] && cp /tmp/dev-orch/e10.md "$SANDBOX/docs/ai-enhancement.md"

# Verify what's in sandbox
BACKEND_FILES=$(find "$SANDBOX/backend" -type f 2>/dev/null | wc -l | tr -d ' ')
FRONTEND_FILES=$(find "$SANDBOX/frontend" -type f 2>/dev/null | wc -l | tr -d ' ')
DOCS_FILES=$(find "$SANDBOX/docs" -type f 2>/dev/null | wc -l | tr -d ' ')

# Write decision
cat > "$SHARED/decisions/${RUN_ID}_${PROJECT}_dev.md" <<END
# Dev Orchestrator Run — $PROJECT ($RUN_ID)

**Priority**: $PRIO_ID — $PRIO_TITLE
**Sandbox**: $SANDBOX

## Output (sandboxed — not in real code yet)
- Backend:  $BACKEND_FILES files  ($(du -sh $SANDBOX/backend 2>/dev/null | awk '{print $1}'))
- Frontend: $FRONTEND_FILES files  ($(du -sh $SANDBOX/frontend 2>/dev/null | awk '{print $1}'))
- Design:   $DOCS_FILES files  ($(du -sh $SANDBOX/docs 2>/dev/null | awk '{print $1}'))

## Sub-agents
- E1-Backend  (groq/llama-3.1-8b): rc=$E1_RC
- E2-Frontend (groq/llama-3.1-8b): rc=$E2_RC
- E10-AI-Design (granite4-local): rc=$E10_RC

## Next step
Human or Claude reviewer should evaluate sandbox and merge selected pieces into real codebase.
END

echo "Dev orchestrator done. Sandbox: $BACKEND_FILES backend, $FRONTEND_FILES frontend, $DOCS_FILES docs files"
echo "[$(date +%H:%M)] DONE backend=$BACKEND_FILES frontend=$FRONTEND_FILES docs=$DOCS_FILES" >> "$LOG"
