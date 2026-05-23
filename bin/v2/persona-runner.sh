#!/usr/bin/env bash
# Surrogate-1 v2 — generic persona runner.
#
# One call = one persona × one task = one artifact written + auto-committed.
# Picks task from per-role backlog OR rotates pre-seeded prompts. Output to
# /data/personae/{role}/{date}-{N}.md, auto-commits to a git repo if
# present, pushes to GitHub if remote configured.
#
# Usage:
#   persona-runner.sh ceo                              # rotate from backlog
#   persona-runner.sh cto "ADR for vector DB choice"   # specific task
#   persona-runner.sh --rotate                         # pick a random role
#
# Reads: bin/v2/personae-registry.json
# Writes: /data/personae/<role>/<date>-<seq>.md
# Logs: ~/.surrogate/logs/persona-<role>.log
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

REGISTRY="$HOME/.surrogate/hf-space/bin/v2/personae-registry.json"
[[ ! -f "$REGISTRY" ]] && REGISTRY="$HOME/.surrogate/bin/v2/personae-registry.json"
OUT_BASE="${OUT_BASE:-${HOME}/.surrogate/data/personae}"
LOG_DIR="$HOME/.surrogate/logs"
mkdir -p "$OUT_BASE" "$LOG_DIR"

ROLE="${1:-}"
TASK="${2:-}"

if [[ "$ROLE" == "--rotate" || -z "$ROLE" ]]; then
    # Pick role by epoch minute (deterministic, all roles get cycled through)
    ROLE=$(python3 -c "
import json, time
with open('$REGISTRY') as f: d = json.load(f)
roles = sorted(d.get('roles', {}).keys())
print(roles[int(time.time() / 60) % len(roles)])
")
fi

# Validate + read role config
ROLE_CFG=$(python3 -c "
import json, sys
with open('$REGISTRY') as f: d = json.load(f)
r = d.get('roles', {}).get('$ROLE')
if not r:
    print(f'ROLE_NOT_FOUND: $ROLE', file=sys.stderr)
    sys.exit(2)
print(json.dumps({'system': r.get('system',''),
                  'cluster': r.get('cluster',''),
                  'templates': r.get('templates', [])}))
") || { echo "❌ unknown role: $ROLE" >&2; exit 2; }

SYSTEM=$(echo "$ROLE_CFG" | python3 -c "import json,sys; print(json.load(sys.stdin)['system'])")
CLUSTER=$(echo "$ROLE_CFG" | python3 -c "import json,sys; print(json.load(sys.stdin)['cluster'])")
TEMPLATES=$(echo "$ROLE_CFG" | python3 -c "import json,sys; print(','.join(json.load(sys.stdin)['templates']))")

# If no task specified, generate one from role's templates
if [[ -z "$TASK" ]]; then
    TPL=$(echo "$TEMPLATES" | tr ',' '\n' | shuf -n1 2>/dev/null || echo "$TEMPLATES" | cut -d, -f1)
    TASK="Produce one '$TPL' artifact for axentx (Surrogate-1 startup). Pick a concrete sub-topic from current axentx priorities."
fi

OUT_DIR="$OUT_BASE/$ROLE"
mkdir -p "$OUT_DIR"
SEQ=$(date +%H%M%S)
OUT="$OUT_DIR/$(date +%Y%m%d)-${SEQ}.md"
LOG="$LOG_DIR/persona-${ROLE}.log"

echo "[$(date +%H:%M:%S)] role=$ROLE cluster=$CLUSTER task='${TASK:0:80}'" >> "$LOG"

# Compose prompt
PROMPT=$(cat <<EOF
$SYSTEM

# Task
$TASK

# Output
- Markdown only
- Concrete, no fluff
- Cite real APIs / frameworks / standards (no phantom libs)
- 300-1200 words
- Header at top: "# $ROLE artifact ($(date -u +%Y-%m-%d))"
EOF
)

# Call ladder — prefer ZeroGPU (free PRO), fall back to cloud APIs
LADDER_BRIDGES=(
    "$HOME/.surrogate/bin/zero-gpu-bridge.sh"
    "$HOME/.surrogate/bin/cerebras-bridge.sh"
    "$HOME/.surrogate/bin/groq-bridge.sh"
    "$HOME/.surrogate/bin/gemini-bridge.sh"
    "$HOME/.surrogate/bin/hf-inference-bridge.sh"
    # "$HOME/.surrogate/bin/chutes-bridge.sh"  # disabled 2026-04-30: chutes 402 free-tier dead
    # On anchor:
    "$HOME/.surrogate/hf-space/bin/anchor/local-llm-bridge.sh"
)

OUTPUT=""
for b in "${LADDER_BRIDGES[@]}"; do
    [[ ! -x "$b" ]] && continue
    out=$(echo -e "$PROMPT" | bash "$b" --max-tokens 2000 2>>"$LOG")
    if [[ -n "$out" ]] && (( ${#out} > 200 )); then
        OUTPUT="$out"
        echo "[$(date +%H:%M:%S)] role=$ROLE bridge=$(basename $b) bytes=${#out}" >> "$LOG"
        break
    fi
done

if [[ -z "$OUTPUT" ]]; then
    echo "[$(date +%H:%M:%S)] FAIL — all bridges empty" >> "$LOG"
    exit 1
fi

# Write artifact + frontmatter
{
    echo "---"
    echo "role: $ROLE"
    echo "cluster: $CLUSTER"
    echo "task: $(echo "$TASK" | head -c 200)"
    echo "generated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "templates_pool: [$TEMPLATES]"
    echo "---"
    echo
    echo "$OUTPUT"
} > "$OUT"

echo "[$(date +%H:%M:%S)] role=$ROLE → $OUT" >> "$LOG"

# Auto-commit + auto-push if /data/personae is a git repo
if [[ -d "$OUT_BASE/.git" ]]; then
    cd "$OUT_BASE"
    git add "$ROLE/" >/dev/null 2>&1
    git -c user.name="Surrogate-1" -c user.email="surrogate-1@axentx.dev" \
        commit -m "$ROLE: $(echo "$TASK" | head -c 60)..." >/dev/null 2>&1
    git push origin main >/dev/null 2>&1 &
    disown
fi

# Append as training pair candidate (for SFT corpus)
TRAIN_FILE="$HOME/.surrogate/data/v2/personae-training.jsonl"
mkdir -p "$(dirname "$TRAIN_FILE")"
python3 -c "
import json
row = {
    'prompt': '''$TASK''',
    'response': open('$OUT').read(),
    'source': 'persona-runner-$ROLE',
    'meta': {'role': '$ROLE', 'cluster': '$CLUSTER',
              'templates_pool': '''$TEMPLATES'''.split(',')}
}
print(json.dumps(row, ensure_ascii=False))
" >> "$TRAIN_FILE" 2>/dev/null || true

# Stdout: just the path so callers can chain
echo "$OUT"
