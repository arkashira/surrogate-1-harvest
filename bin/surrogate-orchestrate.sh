#!/usr/bin/env bash
# Auto-Dev orchestration — chains role-prompts to produce concrete artifacts.
# Bypasses LLM tool-loop (which is unreliable) — uses marker extraction instead.
# Each stage writes a markdown artifact; final stages may emit code patches.
#
# Usage:
#   surrogate-orchestrate.sh "task description"
#   surrogate-orchestrate.sh --mode plan  "task"   # SA + architect only
#   surrogate-orchestrate.sh --mode yolo  "task"   # full chain, no gates
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

MODE="auto"
TASK=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        *)      TASK="$*"; break ;;
    esac
done
[[ -z "$TASK" ]] && { echo "need task"; exit 2; }

# Colors
R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'
CY=$'\033[36m'; GR=$'\033[32m'; YE=$'\033[33m'; MA=$'\033[35m'; RE=$'\033[31m'; GY=$'\033[90m'
BCY=$'\033[96m'

SESSION_ID=$(date +%s | tail -c 9)
WORKDIR="$HOME/.surrogate/state/orchestrate/$SESSION_ID"
TRAINING_LOG="$HOME/.surrogate/training-pairs.jsonl"
mkdir -p "$WORKDIR" "$(dirname "$TRAINING_LOG")"

echo "${BCY}${B}╭─ Auto-Dev Orchestration ─────────────────╮${R}"
echo "${BCY}${B}│${R} session: ${YE}$SESSION_ID${R}  mode: ${MA}$MODE${R}"
echo "${BCY}${B}│${R} cwd: ${D}$(pwd)${R}"
echo "${BCY}${B}╰──────────────────────────────────────────╯${R}"
echo "${B}▸ Task:${R} $TASK"
echo ""

# ── Web research preamble: if task mentions tech we don't recognize, search first ──
RESEARCH_CONTEXT=""
RESEARCH_OUT="$WORKDIR/0-research-context.md"
if echo "$TASK" | grep -iqE "migrat|integrat|switch from|move to|adopt|setup|deploy"; then
    echo "${MA}${B}═══ Stage 0/6: WEB RESEARCH${R} ${D}— gather current docs first${R}"
    python3 - "$TASK" "$RESEARCH_OUT" <<'PYEOF' 2>&1 | sed 's/^/  /' || true
import sys, urllib.request, urllib.parse, json, re, os
task, out_path = sys.argv[1], sys.argv[2]
# Extract tech keywords (capitalized words, dot-versions, snake-case)
keywords = re.findall(r'\b[A-Z][a-zA-Z0-9]{2,}\b|\b[a-z][a-z0-9-]{3,}(?=\s)', task)
keywords = [k for k in keywords if k.lower() not in {'the','this','that','from','with','into','what','when','where','typescript','python','javascript','java','rust'}]
keywords = list(dict.fromkeys(keywords))[:3]  # top-3 unique
if not keywords:
    print("  no clear tech keywords — skipping research")
    sys.exit(0)
print(f"  keywords: {keywords}")
ddg_url = f"https://duckduckgo.com/html/?q={urllib.parse.quote(' '.join(keywords) + ' best practices 2025')}"
try:
    req = urllib.request.Request(ddg_url, headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode('utf-8', errors='ignore')
    # Extract result snippets
    snippets = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', html)[:5]
    titles = re.findall(r'class="result__title"[^>]*>.*?>([^<]+)<', html, re.DOTALL)[:5]
    with open(out_path, 'w') as f:
        f.write(f"# Web research: {' / '.join(keywords)}\n\n")
        for i, (t, s) in enumerate(zip(titles, snippets)):
            f.write(f"## {i+1}. {t.strip()}\n{s.strip()}\n\n")
    print(f"  wrote {len(snippets)} snippets → {os.path.basename(out_path)}")
except Exception as e:
    print(f"  research skipped: {type(e).__name__}: {str(e)[:80]}")
PYEOF
    [[ -f "$RESEARCH_OUT" ]] && RESEARCH_CONTEXT="

=== Web research context ===
$(cat "$RESEARCH_OUT")
=== End research ==="
    echo ""
fi

# ── PRD context: read surrogate.md if present ──
PRD_CONTEXT=""
for prd_file in "$(pwd)/surrogate.md" "$(pwd)/SURROGATE.md"; do
    if [[ -f "$prd_file" ]]; then
        PRD_CONTEXT="

=== Project PRD (surrogate.md) ===
$(head -c 6000 "$prd_file")
=== End PRD ==="
        break
    fi
done

# ── Helper: call LLM directly (skip surrogate -p agent loop entirely) ──
# Why: agent loop forces tool-use system prompt → models output tool-call attempts
# instead of clean markdown deliverables. Direct LLM call gives reliable text-in/text-out.
call_agent() {
    local role="$1" prompt="$2" output_file="$3"
    echo "${CY}▶${R} ${B}$role${R} ${D}working...${R}"

    local prior_artifacts=""
    if [[ -d "$WORKDIR" ]]; then
        prior_artifacts=$(ls -1 "$WORKDIR" 2>/dev/null | grep -v '\.raw$' | sed 's/^/  - /')
    fi

    # Write prompt to temp file (avoids bash quoting hell with multi-KB prompts)
    local prompt_file="$WORKDIR/.prompt-${role//[^a-zA-Z0-9]/_}.txt"
    cat > "$prompt_file" <<EOF
ROLE: $role

$prompt
${RESEARCH_CONTEXT}
${PRD_CONTEXT}
${REPO_CONTEXT}
${RAG_CONTEXT}

=== Working context ===
CWD: $(pwd)
Prior artifacts in $WORKDIR/:
${prior_artifacts:-  (none yet)}

=== OUTPUT FORMAT ===
Write your full deliverable as markdown directly. The wrapper saves your output verbatim.
- Be substantive (≥ 30 lines)
- For DEV role: include code as headings + fenced blocks like:
    ### path/to/file.ext
    \`\`\`<lang>
    <full file content>
    \`\`\`
- No preamble. Begin with a heading.
EOF

    # Direct LLM ladder: tries free fast providers first, paid last.
    # Reads keys from environment to avoid bash quoting nightmares.
    local content
    content=$(GEMINI_KEY="${GEMINI_API_KEY:-}" \
              GEMINI_KEY2="${GEMINI_API_KEY_2:-}" \
              GROQ_KEY="${GROQ_API_KEY:-}" \
              CEREBRAS_KEY="${CEREBRAS_API_KEY:-}" \
              SAMBA_KEY="${SAMBANOVA_API_KEY:-}" \
              CHUTES_KEY="${CHUTES_API_KEY:-}" \
              OR_KEY_ENV="${OPENROUTER_API_KEY:-}" \
              GH_POOL="${GITHUB_TOKEN_POOL:-}" \
              python3 - "$prompt_file" <<'PYEOF' 2>&1
import sys, json, urllib.request, os
from pathlib import Path
prompt = Path(sys.argv[1]).read_text()

def gemini(key, model="gemini-2.5-flash"):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {"contents":[{"parts":[{"text":prompt}]}],
            "generationConfig":{"temperature":0.3,"maxOutputTokens":8192}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
        return d["candidates"][0]["content"]["parts"][0]["text"]

def oai_compatible(url, model, key, extra_headers=None):
    body = {"model":model,"messages":[{"role":"user","content":prompt}],
            "temperature":0.3,"max_tokens":8000}
    headers = {"Content-Type":"application/json","Authorization":f"Bearer {key}"}
    if extra_headers: headers.update(extra_headers)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
        return d["choices"][0]["message"]["content"]

ladder = []
# Long-context priority: when prompt > 20k chars, prefer 128k+ context models
prompt_len = len(prompt)
needs_long_ctx = prompt_len > 20000

# HF Inference Providers PRIORITIZED for code work (highest quality + long context)
# Qwen3-Coder-480B = 262k native context, best OSS coder
# DeepSeek-V3.1-Terminus = 164k context
# GPT-OSS-120B = 128k context
hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
if hf_token:
    # Long-context capable models first (priority order matters — first that succeeds is used)
    long_ctx_models = [
        "Qwen/Qwen3-Coder-480B-A35B-Instruct",   # 262k context, top OSS coder
        "deepseek-ai/DeepSeek-V3.1-Terminus",    # 164k context
        "openai/gpt-oss-120b",                   # 128k context
        "Qwen/Qwen3-235B-A22B-Instruct-2507",    # 128k context
    ]
    for hf_model in long_ctx_models:
        ladder.append((f"hf-router:{hf_model.split('/')[-1][:30]}",
            lambda m=hf_model: oai_compatible(
                "https://router.huggingface.co/v1/chat/completions",
                m, hf_token)))

# Free, fast (Groq + Cerebras serve Llama 3.3 70B at ~500 tok/s) — 128k context
if os.environ.get("CEREBRAS_KEY"):
    ladder.append(("cerebras:llama-70b",
        lambda: oai_compatible("https://api.cerebras.ai/v1/chat/completions",
                               "llama-3.3-70b", os.environ["CEREBRAS_KEY"])))
if os.environ.get("GROQ_KEY"):
    ladder.append(("groq:llama-70b",
        lambda: oai_compatible("https://api.groq.com/openai/v1/chat/completions",
                               "llama-3.3-70b-versatile", os.environ["GROQ_KEY"])))
# Gemini free tier (rotate two keys)
if os.environ.get("GEMINI_KEY"):
    ladder.append(("gemini-1", lambda: gemini(os.environ["GEMINI_KEY"])))
if os.environ.get("GEMINI_KEY2"):
    ladder.append(("gemini-2", lambda: gemini(os.environ["GEMINI_KEY2"])))
# SambaNova free tier (Llama 70B)
if os.environ.get("SAMBA_KEY"):
    ladder.append(("samba:llama-70b",
        lambda: oai_compatible("https://api.sambanova.ai/v1/chat/completions",
                               "Meta-Llama-3.3-70B-Instruct", os.environ["SAMBA_KEY"])))
# GitHub Models (free with PAT, rate-limited)
gh_pool = os.environ.get("GH_POOL", "")
if gh_pool:
    for tok in gh_pool.split(",")[:2]:
        if tok.strip():
            ladder.append(("github-models",
                lambda t=tok.strip(): oai_compatible(
                    "https://models.github.ai/inference/chat/completions",
                    "openai/gpt-4o-mini", t)))
# Chutes (free OSS proxy)
if os.environ.get("CHUTES_KEY"):
    ladder.append(("chutes:qwen3-coder",
        lambda: oai_compatible("https://llm.chutes.ai/v1/chat/completions",
                               "Qwen/Qwen3-Coder-30B-A3B-Instruct", os.environ["CHUTES_KEY"])))
# OpenRouter (paid — only if credit available)
if os.environ.get("OR_KEY_ENV"):
    ladder.append(("or:qwen3-coder",
        lambda: oai_compatible("https://openrouter.ai/api/v1/chat/completions",
                               "qwen/qwen3-coder", os.environ["OR_KEY_ENV"],
                               {"HTTP-Referer":"https://axentx.ai","X-Title":"Surrogate-1"})))
    ladder.append(("or:claude-haiku",
        lambda: oai_compatible("https://openrouter.ai/api/v1/chat/completions",
                               "anthropic/claude-haiku-4.5", os.environ["OR_KEY_ENV"],
                               {"HTTP-Referer":"https://axentx.ai","X-Title":"Surrogate-1"})))

errors, out = [], ""
for name, fn in ladder:
    try:
        result = fn()
        if result and len(result) > 100:
            out = result
            print(f"# generated via {name}", file=sys.stderr)
            break
        errors.append(f"{name}:short({len(result or '')})")
    except urllib.error.HTTPError as e:
        errors.append(f"{name}:HTTP{e.code}")
    except Exception as e:
        errors.append(f"{name}:{type(e).__name__}")

if not out:
    print(f"ERR: providers exhausted ({', '.join(errors[:8])})", file=sys.stderr)
print(out)
PYEOF
)
    # Strip stray markdown wrapping if model added it
    content=$(echo "$content" | sed -E '/^```markdown\s*$/d; /^```\s*$/{ N; /\n```\s*$/d; }' | head -c 60000)

    if [[ -n "$content" ]] && [[ ${#content} -ge 100 ]]; then
        printf '%s\n' "$content" > "$output_file"
        local bytes; bytes=$(wc -c < "$output_file" | tr -d ' ')
        echo "${GR}  ⎿ $role done → $(basename "$output_file") (${bytes} bytes)${R}"
        echo "$content" | head -2 | sed 's/^/    │ /' | cut -c1-110
        push_training_pair "orchestrate-$role" "$prompt" "$content"
        return 0
    else
        printf '%s\n' "$content" > "${output_file}.raw"
        local bytes; bytes=$(wc -c < "${output_file}.raw" 2>/dev/null | tr -d ' ' || echo 0)
        echo "${RE}  ⎿ $role: empty/short — raw saved (${bytes} bytes)${R}"
        echo "$content" | tail -3 | sed 's/^/    │ /' | cut -c1-110
        return 1
    fi
}

# ── Push every task pair to HF training dataset (background) ──
push_training_pair() {
    local source="$1" prompt="$2" content="$3"
    # Central dedup — write only if prompt is new (single source of truth)
    python3 - "$source" "$prompt" "$content" "$TRAINING_LOG" <<'PYEOF' 2>/dev/null &
import sys, json, time, os
sys.path.insert(0, os.path.expanduser("~/.surrogate/bin/lib"))
try:
    from dedup import DedupStore
    HAS_DEDUP = True
except ImportError:
    HAS_DEDUP = False
src, p, c, log = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
if HAS_DEDUP and not DedupStore.is_new(p, source=src):
    sys.exit(0)
pair = {
    'ts': time.time(),
    'source': src,
    'cwd': os.getcwd(),
    'prompt': p[:8000],
    'response': c[:12000],
    'messages': [
        {'role': 'user', 'content': p[:8000]},
        {'role': 'assistant', 'content': c[:12000]},
    ],
}
with open(log, 'a') as f:
    f.write(json.dumps(pair, ensure_ascii=False) + '\n')
PYEOF
    # Trigger HF sync every 25 pairs (background, only if file exists)
    if [[ -f "$TRAINING_LOG" ]]; then
        local count
        count=$(wc -l < "$TRAINING_LOG" 2>/dev/null | tr -d ' ')
        count=${count:-0}
        if [[ $count -gt 0 ]] && [[ $((count % 25)) -eq 0 ]]; then
            nohup bash "$HOME/.local/bin/push-training-to-hf.sh" \
                > "$HOME/.surrogate/logs/training-push.log" 2>&1 &
        fi
    fi
}

# ── Stage 1: SOLUTION ARCHITECT (must run first — blocks everything) ──
SA_OUT="$WORKDIR/1-sa-design.md"
echo "${MA}${B}═══ Stage 1/6: SOLUTION ARCHITECT${R} ${D}— DDD + design patterns${R}"
call_agent "solution-architect" "
You are a senior Solution Architect. Produce a high-level technical design for the task.

Cover (each as a heading):
1. **Bounded contexts** (DDD) — which subdomain(s) does this touch?
2. **Domain model** — entities, aggregates, value objects, repositories
3. **Design patterns** — pick deliberately (Repository / Factory / Strategy / Observer / Builder), justify each
4. **Architecture style** — hexagonal / MVC / clean — show layer flow
5. **Integration points** — APIs, events, side-effects (mermaid diagram welcome)
6. **Non-functional impacts** — perf, security, scale, observability
7. **Risks + mitigations**

Be concrete. No platitudes.

Task: $TASK
" "$SA_OUT"

# ── Stages 2 + 3 in PARALLEL — both depend only on SA, independent of each other ──
ARCH_OUT="$WORKDIR/2-architect-plan.md"
TDD_OUT="$WORKDIR/3-qa-tdd-tests.md"
echo ""
echo "${MA}${B}═══ Stages 2+3 (parallel): ARCHITECT │ QA-TDD${R}"

(
    call_agent "architect" "
You are the Tech Architect. Take the SA design (at $SA_OUT) and produce a CONCRETE file-level execution plan.

Required headings:
1. **Files to create/modify** — exact paths + one-line purpose each
2. **Function signatures** — public APIs with types
3. **Test files first (TDD)** — test cases BEFORE implementation files
4. **Dependencies** — new packages and versions
5. **Migration plan** — schema/config rollouts
6. **Rollback** — how to undo on prod failure

Task: $TASK
" "$ARCH_OUT"
) &
PID_ARCH=$!

(
    call_agent "qa" "
You are the QA Engineer practicing TDD. Output FAILING test code BEFORE the dev writes any implementation.

Inputs:
- SA design: $SA_OUT (read it for design context)

Required output:
1. List of test file paths
2. Full test code for each file as fenced code blocks (\`\`\`python / \`\`\`typescript / etc.)
3. Each test: one assertion, factory functions for fixtures, descriptive name
4. Cover: happy path, edge cases, error paths, security boundaries
5. End with: 'tests will fail because <reason>' for each file

NO implementation code — only tests.

Task: $TASK
" "$TDD_OUT"
) &
PID_QA=$!

wait $PID_ARCH $PID_QA
echo "${D}  parallel stages 2+3 complete${R}"

if [[ "$MODE" == "plan" ]]; then
    echo ""
    echo "${B}▸ Plan-only mode — stopping after architect${R}"
    [[ -f "$ARCH_OUT" ]] && cat "$ARCH_OUT"
    exit 0
fi

# ── Stage 4: DEV (with MoA consensus + self-correction) ──
DEV_OUT="$WORKDIR/4-dev-summary.md"
echo ""
echo "${MA}${B}═══ Stage 4/6: DEV${R} ${D}— implement to green (MoA consensus enabled)${R}"

# Use MoA consensus for DEV stage by default — 3 LLMs propose, judge synthesizes
# Higher quality at 4× cost. Override with ENABLE_MOA=0 if needed.
ENABLE_MOA="${ENABLE_MOA:-1}"

DEV_PROMPT="
You are the Senior Developer. Make the QA tests PASS by implementing per the Architect plan.

Inputs:
- SA design:    $SA_OUT
- Architect:    $ARCH_OUT
- QA tests:     $TDD_OUT

Output (markdown):
1. Heading per file: \`### path/to/file.ext\`
2. Below each heading: full file content as fenced \`\`\`<lang> code block
3. End with: '### Summary' — list of files + 'tests now pass because <reason>'

Rules:
- Implement ONLY what's needed to pass tests (red → green → refactor)
- DDD: Repository for data access, no business logic in handlers
- Apply patterns from SA design (Strategy/Factory/Observer/etc.)
- Type-strict (TS strict / Python type hints / Go generics)
- Result/Either pattern over throws for expected errors
- Intent-revealing names; units in numerics
- NO commented-out code, NO TODO without ticket ID, NO hallucinated imports
- MATCH existing codebase style from REPO CONTEXT above
- Use REAL imports from REPO CONTEXT — don't invent new ones

Task: $TASK
"

if [[ "$ENABLE_MOA" == "1" ]] && [[ -x "$HOME/.surrogate/bin/moa-consensus.py" ]]; then
    echo "${D}  using MoA consensus (3 propose + 1 judge)${R}"
    DEV_PROMPT_FILE="$WORKDIR/.dev-prompt.txt"
    echo "$DEV_PROMPT" > "$DEV_PROMPT_FILE"
    GEMINI_KEY="${GEMINI_API_KEY:-}" \
    GEMINI_KEY2="${GEMINI_API_KEY_2:-}" \
    GROQ_KEY="${GROQ_API_KEY:-}" \
    CEREBRAS_KEY="${CEREBRAS_API_KEY:-}" \
    HF_TOKEN="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}" \
        python3 "$HOME/.surrogate/bin/moa-consensus.py" "$DEV_PROMPT_FILE" "dev" > "$DEV_OUT" 2>>"$WORKDIR/dev-stderr.log"
    if [[ -s "$DEV_OUT" ]]; then
        echo "${GR}  ⎿ dev (MoA) done → $(basename "$DEV_OUT") ($(wc -c < "$DEV_OUT" | tr -d ' ') bytes)${R}"
        head -2 "$DEV_OUT" | sed 's/^/    │ /' | cut -c1-110
        # Push as training pair
        push_training_pair "orchestrate-dev-moa" "$DEV_PROMPT" "$(cat "$DEV_OUT")"
    else
        echo "${YE}  ⎿ MoA empty — falling back to single-model${R}"
        call_agent "dev" "$DEV_PROMPT" "$DEV_OUT"
    fi
else
    call_agent "dev" "$DEV_PROMPT" "$DEV_OUT"
fi

# ── Self-correction: if QA detects failure, retry DEV with error context ──
# Run quick test extraction + execution check on the dev output
DEV_RETRIES=0
while [[ $DEV_RETRIES -lt 2 ]]; do
    # Extract code blocks; check for obvious issues (syntax, missing imports)
    SELF_CHECK=$(python3 - "$DEV_OUT" <<'PYEOF' 2>/dev/null
import sys, re
out = open(sys.argv[1]).read()
issues = []
# Detect Python files and try parsing them
for m in re.finditer(r'###\s+([^\n]+\.py)\s*\n+```python\n(.*?)^```', out, re.MULTILINE | re.DOTALL):
    path, code = m.group(1).strip(), m.group(2)
    try:
        compile(code, path, 'exec')
    except SyntaxError as e:
        issues.append(f"SyntaxError in {path}:{e.lineno} — {e.msg}")
    # Detect commonly hallucinated imports
    for imp in re.findall(r'^from\s+(\w[\w.]*)\s+import|^import\s+(\w[\w.]*)', code, re.MULTILINE):
        mod = imp[0] or imp[1]
        if mod.startswith(('app.', 'src.', 'core.', 'lib.', 'utils.')):
            # Check if module exists in repo context (rough heuristic)
            pass  # skip — too noisy
print('\n'.join(issues) if issues else 'OK')
PYEOF
)
    if [[ "$SELF_CHECK" == "OK" ]]; then
        break
    fi
    DEV_RETRIES=$((DEV_RETRIES + 1))
    echo "${YE}  ⎿ self-check found issues (retry $DEV_RETRIES/2):${R}"
    echo "$SELF_CHECK" | head -5 | sed 's/^/      /'
    # Retry with error context
    RETRY_PROMPT="$DEV_PROMPT

=== PREVIOUS ATTEMPT FAILED — FIX THESE ISSUES ===
$SELF_CHECK

Generate corrected version. Same output format."
    call_agent "dev-retry-$DEV_RETRIES" "$RETRY_PROMPT" "$DEV_OUT"
done

# Extract code blocks from DEV output → write actual files
if [[ -f "$DEV_OUT" ]]; then
    echo "${D}  Extracting code blocks → real files${R}"
    python3 - "$DEV_OUT" "$(pwd)" <<'PYEOF' 2>&1 | sed 's/^/    /'
import sys, re, os
from pathlib import Path
md_path, cwd = sys.argv[1], sys.argv[2]
md = Path(md_path).read_text()
# Match: ### relative/path.ext  followed by ```lang ... ```
pattern = re.compile(r'^###\s+([^\s]+\.[a-zA-Z0-9]+)\s*$\n+```[a-zA-Z0-9_+-]*\n(.*?)^```\s*$', re.MULTILINE | re.DOTALL)
written = 0
for m in pattern.finditer(md):
    rel = m.group(1).strip()
    code = m.group(2)
    if rel.startswith('/'):
        target = Path(rel)
    else:
        target = Path(cwd) / rel
    # Safety: refuse paths escaping cwd
    try:
        target = target.resolve()
        Path(cwd).resolve().relative_to(Path(cwd).resolve())  # no-op
        if not str(target).startswith(str(Path(cwd).resolve())):
            print(f"  skip (outside cwd): {rel}")
            continue
    except Exception:
        continue
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code)
    written += 1
    print(f"  wrote {rel} ({len(code)} bytes)")
print(f"  total {written} files written")
PYEOF
fi

# ── Stages 5 + 6a in PARALLEL — both depend on dev, independent of each other ──
QA_OUT="$WORKDIR/5-qa-verify.md"
OPS_OUT="$WORKDIR/6a-ops-checklist.md"
NEED_OPS=0
if echo "$TASK" | grep -iqE "deploy|docker|helm|k8s|terraform|cicd|ci/cd|cloudformation|buildspec|ecs|lambda"; then
    NEED_OPS=1
fi

echo ""
if [[ $NEED_OPS -eq 1 ]]; then
    echo "${MA}${B}═══ Stages 5+6a (parallel): QA-VERIFY │ OPS${R}"
else
    echo "${MA}${B}═══ Stage 5/6: QA-VERIFY${R}"
fi

(
    call_agent "qa" "
You are QA in verification phase. Verify the dev's claim that tests pass.

Inputs:
- QA tests written: $TDD_OUT
- Dev summary:      $DEV_OUT

Output:
1. **Run results** — what command(s) you'd run, expected pass/fail
2. **Coverage** — branches covered, gaps identified
3. **Lint/type** — checks performed
4. **Verdict** — READY / NEEDS-WORK with specific gaps

Task: $TASK
" "$QA_OUT"
) &
PID_QA2=$!

if [[ $NEED_OPS -eq 1 ]]; then
    (
        call_agent "ops" "
Review infrastructure aspects of this task.
- Dockerfile / helm / terraform / cloudformation validity
- Secrets / env var handling
- Resource limits + cost guardrails
- Observability (metrics/logs/traces)
- IAM least privilege

Inputs: $DEV_OUT
Task: $TASK
" "$OPS_OUT"
    ) &
    PID_OPS=$!
    wait $PID_QA2 $PID_OPS
else
    wait $PID_QA2
    echo "${GY}═══ Stage 6a/6: OPS — skipped (not infra task)${R}"
fi
echo "${D}  parallel stages 5+6a complete${R}"

# ── Stage 6: REVIEWER ──
REVIEW_OUT="$WORKDIR/6-review-verdict.md"
echo ""
echo "${MA}${B}═══ Stage 6/6: REVIEWER${R} ${D}— final gate${R}"
call_agent "reviewer" "
FINAL REVIEW GATE. Inspect prior stages and judge.

Inputs:
- Architect: $ARCH_OUT
- Dev:       $DEV_OUT
- QA:        $QA_OUT

Judge on:
1. Correctness vs requirements
2. Code quality (naming, no hallucinated imports, error handling)
3. Security (no secret leakage, input validation)
4. Test coverage
5. Match existing codebase style

Output format:
**Verdict:** APPROVE | REWORK | REJECT
**Reasons:** (3–5 bullets)
**Action items if REWORK:** (specific fixes)

Task: $TASK
" "$REVIEW_OUT"

# ── Summary + auto-commit on APPROVE ──
echo ""
echo "${BCY}${B}╭─ Session Complete ───────────────────────╮${R}"
echo "${BCY}${B}│${R} session: $SESSION_ID"
echo "${BCY}${B}│${R} artifacts: $WORKDIR/"
echo "${BCY}${B}╰──────────────────────────────────────────╯${R}"
ls -la "$WORKDIR/" 2>&1 | tail -n +2 | awk '{printf "  %s  %s\n", $5, $9}' | grep -v '   $'

VERDICT_TEXT=""
if [[ -f "$REVIEW_OUT" ]]; then
    VERDICT_TEXT=$(grep -iE "verdict|APPROVE|REWORK|REJECT" "$REVIEW_OUT" | head -3)
    echo ""
    echo "${B}▸ Final verdict:${R}"
    echo "$VERDICT_TEXT" | sed 's/^/  /'
fi

if echo "$VERDICT_TEXT" | grep -qi "APPROVE"; then
    echo ""
    echo "${GR}${B}▸ Reviewer approved — committing changes${R}"
    if ! git -C "$(pwd)" diff --quiet 2>/dev/null || ! git -C "$(pwd)" diff --cached --quiet 2>/dev/null; then
        git -C "$(pwd)" add -A 2>/dev/null
        SHORT_TASK=$(echo "$TASK" | head -c 72)
        if git -C "$(pwd)" commit -m "feat: $SHORT_TASK

[surrogate auto-dev session $SESSION_ID]
[reviewed: APPROVE]" 2>&1 | tee -a "$WORKDIR/git-commit.log" | grep -q "master\|main\|\["; then
            COMMIT_HASH=$(git -C "$(pwd)" rev-parse --short HEAD 2>/dev/null)
            echo "${GR}  ✅ Committed: $COMMIT_HASH${R}"
        else
            echo "${YE}  ⚠ Nothing to commit${R}"
        fi
    else
        echo "${GY}  ○ No file changes to commit${R}"
    fi
elif echo "$VERDICT_TEXT" | grep -qi "REWORK"; then
    echo ""
    echo "${YE}${B}▸ Reviewer requested REWORK — re-run orchestrate after addressing notes${R}"
    grep -A5 -i "REWORK\|action item" "$REVIEW_OUT" | head -10 | sed 's/^/  /'
fi
