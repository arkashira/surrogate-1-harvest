#!/usr/bin/env bash
# AgentEvals-style trajectory scoring — evaluates worker OUTPUT STRUCTURE against
# a reference trajectory template per task type. Deterministic scoring 0-1.
# Fails the gate if path/structure score < threshold (not just final output quality).
# Runs after validator, before reviewer. Adds .trajectory_score.json per output.
set -u

LOG="$HOME/.claude/logs/agentevals-score.log"
REF_DIR="$HOME/.claude/memory/trajectory-refs"
REVIEW_DIR="$HOME/.hermes/workspace/qwen-coder-reviews"
OUT_DIRS=(
    "$HOME/.hermes/workspace/qwen-coder"
    "$HOME/.hermes/workspace/dev-cloud-github"
    "$HOME/.hermes/workspace/dev-cloud-samba"
    "$HOME/.hermes/workspace/dev-cloud-cloudflare"
    "$HOME/.hermes/workspace/dev-cloud-groq"
    "$HOME/.hermes/workspace/dev-cloud-gemini"
    "$HOME/.hermes/workspace/dev-cloud-synthesis"
)
mkdir -p "$(dirname "$LOG")" "$REF_DIR" "$REVIEW_DIR"

# Seed 3 reference trajectory templates (structure expectations per task)
if [[ ! -f "$REF_DIR/coding-task.json" ]]; then
    cat > "$REF_DIR/coding-task.json" <<'EOF'
{
  "task_type": "coding",
  "required_sections": [
    "Implementation Plan",
    "Code",
    "Tests",
    "Acceptance Criteria"
  ],
  "required_code_blocks": {"min": 2, "max": 5, "languages": ["python","typescript","javascript","go","rust","bash"]},
  "min_output_length": 800,
  "max_output_length": 15000,
  "forbidden_patterns": ["# TODO:", "// TODO:", "<INSERT", "\\[PLACEHOLDER\\]", "pass  #"],
  "required_patterns": ["def |function |class |const |let "],
  "weight": 1.0
}
EOF
fi
if [[ ! -f "$REF_DIR/discovery-task.json" ]]; then
    cat > "$REF_DIR/discovery-task.json" <<'EOF'
{
  "task_type": "discovery",
  "required_sections": ["Pain", "Persona", "JTBD"],
  "min_output_length": 1500,
  "required_patterns": ["https?://", "Evidence:", "Persona:"],
  "weight": 1.0
}
EOF
fi
if [[ ! -f "$REF_DIR/ceremony-task.json" ]]; then
    cat > "$REF_DIR/ceremony-task.json" <<'EOF'
{
  "task_type": "ceremony",
  "required_sections": ["Yesterday|Today|Blockers|Priority|Goal"],
  "min_output_length": 300,
  "max_output_length": 5000,
  "weight": 0.8
}
EOF
fi

# Score each unscored output from last 30 min
SCORED=0
for DIR in "${OUT_DIRS[@]}"; do
    [[ -d "$DIR" ]] || continue
    for FILE in $(find "$DIR" -maxdepth 1 -name '*.md' -mmin -30 2>/dev/null); do
        BASENAME=$(basename "$FILE" .md)
        SCORE_FILE="$REVIEW_DIR/${BASENAME}.trajectory_score.json"
        [[ -f "$SCORE_FILE" ]] && continue

        /usr/bin/python3 <<PYEOF > "$SCORE_FILE"
import json, re
from pathlib import Path

with open("$FILE") as f: content = f.read()

# Identify task type: dev-cloud-* or qwen-coder → coding; ceremonies/pain-research → discovery; others → ceremony
path = "$DIR"
if 'dev-cloud' in path or 'qwen-coder' in path:
    ref = json.load(open("$REF_DIR/coding-task.json"))
elif 'pain-research' in path or 'design' in path or 'market' in path:
    ref = json.load(open("$REF_DIR/discovery-task.json"))
else:
    ref = json.load(open("$REF_DIR/ceremony-task.json"))

# Run checks
checks = {}

# 1. Required sections
if ref.get('required_sections'):
    for sec in ref['required_sections']:
        # sec can be pipe-separated alternatives
        patterns = sec.split('|')
        found = any(re.search(rf'##?\s+{re.escape(p)}', content, re.IGNORECASE) for p in patterns)
        checks[f'section_{sec[:20]}'] = 1.0 if found else 0.0

# 2. Code blocks count + language
if 'required_code_blocks' in ref:
    blocks = re.findall(r'\`\`\`(\w*)\n', content)
    rc = ref['required_code_blocks']
    checks['code_block_count'] = 1.0 if rc['min'] <= len(blocks) <= rc['max'] else 0.5 if blocks else 0.0
    if blocks and rc.get('languages'):
        lang_ok = any(b.lower() in rc['languages'] for b in blocks if b)
        checks['code_block_lang'] = 1.0 if lang_ok else 0.3

# 3. Length bounds
ln = len(content)
mn = ref.get('min_output_length', 0)
mx = ref.get('max_output_length', 100000)
checks['length'] = 1.0 if mn <= ln <= mx else 0.3 if ln < mn else 0.7

# 4. Forbidden patterns (TODOs, placeholders)
if ref.get('forbidden_patterns'):
    forbidden_count = sum(len(re.findall(p, content, re.IGNORECASE)) for p in ref['forbidden_patterns'])
    checks['no_forbidden'] = 1.0 if forbidden_count == 0 else max(0.0, 1.0 - forbidden_count * 0.2)

# 5. Required patterns
if ref.get('required_patterns'):
    missing = sum(1 for p in ref['required_patterns'] if not re.search(p, content))
    checks['required_patterns'] = 1.0 - (missing / len(ref['required_patterns']))

# Aggregate
weights = ref.get('weight', 1.0)
avg = sum(checks.values()) / len(checks) if checks else 0
final_score = round(avg * weights, 3)
verdict = 'pass' if final_score >= 0.75 else 'warn' if final_score >= 0.5 else 'fail'

result = {
    "basename": "$BASENAME",
    "task_type": ref['task_type'],
    "reference": Path("$REF_DIR").name + f"/{ref['task_type']}-task.json",
    "checks": checks,
    "score": final_score,
    "verdict": verdict,
    "gate_threshold": 0.75,
}
print(json.dumps(result, indent=2))
PYEOF
        SCORED=$((SCORED + 1))
    done
done

echo "[$(date '+%H:%M:%S')] scored $SCORED outputs" >> "$LOG"
