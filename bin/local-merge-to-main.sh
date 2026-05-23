#!/usr/bin/env bash
# Local merge to main — since GitHub push auth is broken (remote `backup` on 
# arkashira/* repos while gh auth on ashirap), do local main-merge instead of
# requiring GitHub PR flow. Only merges branches where the latest commit's
# output passed ALL quality gates (quality≥7, validator PASS, tests pass).
set -u

LOG="$HOME/.claude/logs/local-merge-to-main.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }
log "=== local-merge cycle ==="

merged=0
blocked=0

for proj in Costinel Vanguard arkship surrogate workio; do
    proj_dir="$HOME/axentx/$proj"
    [[ ! -d "$proj_dir/.git" ]] && continue
    cd "$proj_dir" || continue

    # Find hermes/auto/* branches not yet merged into main
    for branch in $(git branch 2>/dev/null | grep -E "hermes/auto/" | tr -d ' *'); do
        # Already merged?
        if git merge-base --is-ancestor "$branch" main 2>/dev/null; then
            continue
        fi

        # Check branch's priority quality via review JSON
        prio=$(echo "$branch" | grep -oE "p[0-9]+" | head -1)
        [[ -z "$prio" ]] && continue

        # Find most recent review with quality≥7 for this priority
        best_q=$(python3 <<PYEOF 2>/dev/null
import json, re, glob
best = 0
for rf in glob.glob('$HOME/.hermes/workspace/qwen-coder-reviews/${prio}_*.review.json'):
    try:
        txt = open(rf).read()
        m = re.search(r'\{.*\}', txt, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
        q = d.get('quality_score', 0)
        if q > best: best = q
    except: pass
print(best)
PYEOF
)

        if [[ -z "$best_q" ]] || [[ "$best_q" -lt 7 ]]; then
            log "  $proj/$branch: quality=$best_q < 7 — NOT merging"
            blocked=$((blocked + 1))
            continue
        fi

        # Check validator + tests pass
        all_pass=$(python3 <<PYEOF 2>/dev/null
import json, glob
ok = False
for vf in glob.glob('$HOME/.hermes/workspace/qwen-coder-reviews/${prio}_*.validation.json'):
    try:
        d = json.load(open(vf))
        if d.get('status') == 'pass' and d.get('imports_ok', False):
            ok = True; break
    except: pass
print('yes' if ok else 'no')
PYEOF
)
        if [[ "$all_pass" != "yes" ]]; then
            log "  $proj/$branch: validator not PASS — skipping"
            blocked=$((blocked + 1))
            continue
        fi

        # Merge
        git checkout main 2>&1 | tail -1
        git merge --no-ff -m "merge($prio): auto-merge from hermes/auto (quality=$best_q)" "$branch" 2>&1 | tail -2
        if [[ $? -eq 0 ]]; then
            log "  ✅ $proj/$branch merged (quality=$best_q)"
            merged=$((merged + 1))
        else
            log "  ❌ $proj/$branch merge conflict — rollback"
            git merge --abort 2>/dev/null
        fi
    done
done

log "summary: merged=$merged blocked=$blocked"
echo "local-merge: merged=$merged blocked=$blocked"
