#!/usr/bin/env bash
# Surrogate Continuous Dev Loop — local 24/7 micro-development.
#
# Picks a real TODO/FIXME or quality issue from user's projects and asks
# the local Surrogate-1 (gemma4-based, free, unlimited) to propose a fix.
# Output goes to ~/.hermes/workspace/local-dev/ for review — does NOT
# auto-edit user code.
#
# Pairs with cloud free-tier daemons (cerebras/groq/etc.) which handle
# heavy multi-step priorities. This loop fills the "always-on" gap with
# small atomic improvements.
#
# Usage:
#   surrogate-dev-loop.sh             # one cycle
#   surrogate-dev-loop.sh --continuous N  # N cycles (default 1)
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/surrogate-dev-loop.log"
OUT_DIR="$HOME/.hermes/workspace/local-dev"
mkdir -p "$(dirname "$LOG")" "$OUT_DIR"

CYCLES="${1:-1}"
[[ "$CYCLES" == "--continuous" ]] && CYCLES="${2:-1}"

# ── Search roots — only user's own projects, not system dirs ─────────────────
SEARCH_ROOTS=(
    "$HOME/axentx"
    "$HOME/develope/DevOps"
    "$HOME/develope/AI"
    "$HOME/.surrogate/bin"
)

# ── Task generators (pick one per cycle, weighted random) ────────────────────
pick_task() {
    python3 <<'PYEOF'
import os, random, re, subprocess, json
from pathlib import Path

ROOTS = [
    Path.home() / 'axentx',
    Path.home() / 'develope/DevOps',
    Path.home() / 'develope/AI',
    Path.home() / '.surrogate/bin',
]
ROOTS = [p for p in ROOTS if p.exists()]

def find_todo():
    """Find a TODO/FIXME/XXX/HACK comment in user code (uses ripgrep — fast)."""
    cmd = ['/opt/homebrew/bin/rg', '--no-heading', '-n', '-m', '3',
           '--type', 'py', '--type', 'sh', '--type', 'ts', '--type', 'go',
           '-g', '!node_modules', '-g', '!.venv', '-g', '!__pycache__',
           '-g', '!.git', '-g', '!dist', '-g', '!build',
           r'(TODO|FIXME|XXX|HACK)[:\s]']
    for root in ROOTS:
        cmd.append(str(root))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        lines = [l for l in r.stdout.splitlines() if l.strip()][:300]
        if not lines:
            return None
        line = random.choice(lines)
        # parse: path:lineno:content
        m = re.match(r'^([^:]+):(\d+):(.+)$', line)
        if not m:
            return None
        path, lineno, content = m.groups()
        return {
            'kind': 'todo',
            'path': path,
            'line': int(lineno),
            'content': content.strip()[:200],
            'task': f"Resolve this TODO in {Path(path).name}:{lineno}\n  {content.strip()[:200]}\nPropose a concrete implementation. Don't auto-edit — just describe the fix.",
        }
    except Exception:
        return None


def find_long_function():
    """Find a Python function >50 lines that may need refactoring."""
    cmd = ['find'] + [str(r) for r in ROOTS] + [
        '-name', '*.py',
        '-not', '-path', '*/node_modules/*',
        '-not', '-path', '*/.venv/*',
        '-not', '-path', '*/__pycache__/*',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        files = r.stdout.splitlines()[:500]
        random.shuffle(files)
        for f in files[:30]:
            try:
                lines = Path(f).read_text(errors='replace').splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines):
                if re.match(r'\s*def\s+\w+', line):
                    indent = len(line) - len(line.lstrip())
                    end = i + 1
                    while end < len(lines):
                        l = lines[end]
                        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
                            break
                        end += 1
                    if end - i > 50:
                        func = '\n'.join(lines[i:min(i+80, end)])
                        return {
                            'kind': 'refactor',
                            'path': f,
                            'line': i + 1,
                            'task': f"This Python function in {Path(f).name}:{i+1} is {end-i} lines long. Suggest 2-3 ways to split it into smaller, more focused functions. Be specific (function names + responsibility).",
                            'context': func[:2500],
                        }
        return None
    except Exception:
        return None


def find_missing_docstring():
    """Find a Python public function without a docstring."""
    cmd = ['find'] + [str(r) for r in ROOTS] + ['-name', '*.py', '-not', '-path', '*/__pycache__/*']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        files = r.stdout.splitlines()[:300]
        random.shuffle(files)
        for f in files[:20]:
            try:
                lines = Path(f).read_text(errors='replace').splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines):
                m = re.match(r'^def\s+([a-z]\w*)\(', line)
                if not m:
                    continue
                # Skip private + dunder
                if m.group(1).startswith('_'):
                    continue
                # Check if next non-blank line is a docstring
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and not lines[j].lstrip().startswith(('"""', "'''")):
                    body_end = min(i + 25, len(lines))
                    func = '\n'.join(lines[i:body_end])
                    return {
                        'kind': 'docstring',
                        'path': f,
                        'line': i + 1,
                        'func_name': m.group(1),
                        'task': f"Write a concise Python docstring for `{m.group(1)}` in {Path(f).name}:{i+1}. Include: one-line summary, Args (with types), Returns. NO Examples section. Output the docstring text only.",
                        'context': func[:1500],
                    }
        return None
    except Exception:
        return None


# Weighted random pick (TODO scan most useful, refactor rare, docstring filler)
generators = [
    (0.55, find_todo),
    (0.20, find_long_function),
    (0.25, find_missing_docstring),
]
random.shuffle(generators)
generators.sort(key=lambda x: random.random())  # extra shuffle

for _, gen in generators:
    task = gen()
    if task:
        print(json.dumps(task, ensure_ascii=False))
        break
PYEOF
}


# ── Reflexion: load top-3 lessons learned for this task kind ────────────────
load_reflexion_lessons() {
    local kind="$1"
    local file="$HOME/.hermes/workspace/reflexion/lessons-${kind}.jsonl"
    [[ ! -f "$file" ]] && { echo ""; return; }
    python3 <<PYEOF
import json
from pathlib import Path
p = Path("$file")
if not p.exists(): exit()
lines = p.read_text().splitlines()[-50:]   # last 50 entries
records = []
for l in lines:
    try: records.append(json.loads(l))
    except: pass
# Score: explicit score first, else recency. Take top 3 unique lessons.
records.sort(key=lambda r: r.get('score', 0), reverse=True)
seen = set(); top = []
for r in records:
    lesson = r.get('lesson','').strip()
    if not lesson or lesson in seen: continue
    seen.add(lesson); top.append(lesson)
    if len(top) >= 3: break
if top:
    print("=== Reflexion: lessons from past attempts ===")
    for i, l in enumerate(top, 1):
        print(f"{i}. {l}")
    print("=== end lessons ===\n")
PYEOF
}

# ── Reflexion: extract & save 1-line lesson from a completed cycle ──────────
save_reflexion_lesson() {
    local kind="$1" task="$2" response="$3" duration="$4"
    local file="$HOME/.hermes/workspace/reflexion/lessons-${kind}.jsonl"
    mkdir -p "$(dirname "$file")"
    # Pass payload via env vars + sys.argv (safe — no shell quoting issues with embedded quotes)
    REFLEX_RESP="$response" REFLEX_TASK="$task" \
        python3 - "$kind" "$duration" "$file" <<'PYEOF'
import json, re, os, sys
from datetime import datetime
kind, dur, out_file = sys.argv[1], int(sys.argv[2]), sys.argv[3]
resp = os.environ.get('REFLEX_RESP', '')
task = os.environ.get('REFLEX_TASK', '')[:200]

lesson = None
for pat in [
    r'(?:lesson|key insight|key takeaway|note):\s*([^\n]{20,200})',
    r'(?:I learned|important to|remember to|need to)\s+([^\n]{20,200})',
]:
    m = re.search(pat, resp, re.IGNORECASE)
    if m: lesson = m.group(1).strip(); break
if not lesson:
    sentences = [s.strip() for s in re.split(r'[\.\n]+', resp) if 30 < len(s.strip()) < 200]
    if sentences: lesson = sentences[0]
if lesson:
    record = {
        'ts': datetime.utcnow().isoformat(),
        'kind': kind, 'task': task, 'lesson': lesson[:300],
        'duration_sec': dur,
        'score': 1.0 if dur < 60 else 0.5,
    }
    with open(out_file, 'a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
PYEOF
}

# ── Run one cycle: pick task, ask Surrogate-1, save output ──────────────────
run_cycle() {
    local cycle_num="$1"
    local task_json
    task_json=$(pick_task)
    if [[ -z "$task_json" ]]; then
        echo "[$(date +%H:%M:%S)] no task found this cycle" >> "$LOG"
        return 0
    fi

    local kind path line task_text context
    kind=$(echo "$task_json" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('kind',''))")
    path=$(echo "$task_json" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('path',''))")
    line=$(echo "$task_json" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('line',0))")
    task_text=$(echo "$task_json" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('task',''))")
    context=$(echo "$task_json" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('context',''))")

    local id="$(date +%s)-${kind}"
    local out="$OUT_DIR/${id}.md"
    local start
    start=$(date +%s)

    echo "[$(date +%H:%M:%S)] cycle=$cycle_num kind=$kind path=$(basename "$path"):$line" >> "$LOG"

    # Build prompt — prepend Reflexion lessons if any
    local lessons
    lessons=$(load_reflexion_lessons "$kind")
    local prompt="${lessons}${task_text}"
    [[ -n "$context" ]] && prompt="$prompt

=== Code context ===
\`\`\`
$context
\`\`\`"

    # Call Surrogate-1 via Ollama (keep_alive=5m so model stays warm between cycles)
    local body
    body=$(PROMPT_VAR="$prompt" python3 <<'PYEOF'
import json, os
print(json.dumps({
    "model": "surrogate-1",
    "messages": [{"role": "user", "content": os.environ["PROMPT_VAR"]}],
    "max_tokens": 1500,
    "temperature": 0.2,
    "top_p": 0.9,
    "keep_alive": "5m",
}))
PYEOF
)
    local resp
    resp=$(curl -sS --max-time 120 \
        http://localhost:11434/v1/chat/completions \
        -H 'Content-Type: application/json' \
        -d "$body" 2>/dev/null)

    local answer
    answer=$(echo "$resp" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d['choices'][0]['message']['content'])
except Exception as e:
    print(f'[err] {e}')
")
    local dur=$(( $(date +%s) - start ))

    # Save output
    cat > "$out" <<EOF
# Local Dev: $kind — $(date '+%Y-%m-%d %H:%M')
**File:** \`$path:$line\`
**Duration:** ${dur}s
**Model:** surrogate-1 (gemma4:e4b base)

---

## Task
$task_text

---

## Surrogate-1 Response
$answer

---

*Auto-generated by surrogate-dev-loop. Review before applying.*
EOF

    echo "[$(date +%H:%M:%S)] cycle=$cycle_num done in ${dur}s → $(basename "$out")" >> "$LOG"

    # Reflexion: extract & save lesson from this cycle
    save_reflexion_lesson "$kind" "$task_text" "$answer" "$dur"

    # Append to training-data candidate (env vars + argv = safe quoting)
    DEV_TASK="$task_text" DEV_ANSWER="$answer" \
        python3 - "$kind" "$dur" <<'PYEOF'
import json, os, sys
from pathlib import Path
from datetime import datetime
kind, dur = sys.argv[1], int(sys.argv[2])
candidate = Path.home() / 'axentx/surrogate/data/training-jsonl/local-dev-pending.jsonl'
candidate.parent.mkdir(parents=True, exist_ok=True)
record = {
    'ts': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    'kind': kind,
    'task': os.environ.get('DEV_TASK', '')[:8000],
    'response': os.environ.get('DEV_ANSWER', '')[:5000],
    'duration_sec': dur,
    'source': 'surrogate-dev-loop',
}
with open(candidate, 'a') as f:
    f.write(json.dumps(record, ensure_ascii=False) + '\n')
PYEOF
}


# ── Main loop ────────────────────────────────────────────────────────────────
echo "[$(date +%H:%M:%S)] dev-loop start cycles=$CYCLES" >> "$LOG"

for i in $(seq 1 "$CYCLES"); do
    run_cycle "$i" || true
    # Small delay between cycles (don't hammer Ollama)
    [[ $i -lt $CYCLES ]] && sleep 30
done

echo "[$(date +%H:%M:%S)] dev-loop done" >> "$LOG"
