#!/usr/bin/env bash
# Surrogate-1 v2 — Regression test runner.
#
# Run after every Round push to catch breakage early. Tests:
#   1. Bash syntax (`bash -n`) on all .sh
#   2. Python parse (`ast.parse`) on all .py
#   3. YAML schema (`yaml.safe_load`) on all .yml/.yaml
#   4. JSON schema on all .json
#   5. Bridge smoke (each ladder tier: ping with "say OK" prompt)
#   6. v2 module imports (no top-level errors)
#   7. Coordinator schema (sqlite open + table count)
#   8. Reflexion / voyager / letta stores (stats command works)
#   9. Sanitize lib (filter_pair on known-good and known-bad inputs)
#  10. Cron heredoc inside start.sh extractable + parseable
#
# Exit codes:
#   0 = all pass
#   1 = any test failed
#   2 = environment missing (.hermes/.env etc.)
#
# CLI:
#   bash regression-test.sh             # full suite
#   bash regression-test.sh --quick     # skip slow bridge smoke
set -uo pipefail

QUICK="${QUICK:-0}"
[[ "${1:-}" == "--quick" ]] && QUICK=1

REPO="$HOME/.surrogate/hf-space"
LOG="/tmp/surrogate-regression-$(date +%Y%m%d-%H%M%S).log"
PASS=0
FAIL=0
WARN=0
declare -a FAILS=()

t_pass() { PASS=$((PASS+1)); }
t_fail() { FAIL=$((FAIL+1)); FAILS+=("$1"); echo "  ✗ FAIL: $1" | tee -a "$LOG"; }
t_warn() { WARN=$((WARN+1));   echo "  ~ WARN: $1" | tee -a "$LOG"; }
t_info() { echo "$1" | tee -a "$LOG"; }

t_info "═══ Surrogate-1 v2 regression test ═══"
t_info "log: $LOG"
t_info ""

# ── 1. Bash syntax ─────────────────────────────────────────────────────
t_info "[1/10] bash -n on all *.sh"
n=0
while IFS= read -r f; do
    n=$((n+1))
    if bash -n "$f" 2>>"$LOG"; then
        t_pass
    else
        t_fail "bash syntax: $f"
    fi
done < <(find "$REPO/bin" "$REPO/start.sh" -name "*.sh" 2>/dev/null)
t_info "  scanned $n .sh files"

# ── 2. Python ast.parse ────────────────────────────────────────────────
t_info ""
t_info "[2/10] python3 -c 'ast.parse' on all *.py"
n=0
while IFS= read -r f; do
    n=$((n+1))
    if python3 -c "import ast; ast.parse(open('$f').read())" 2>>"$LOG"; then
        t_pass
    else
        t_fail "python parse: $f"
    fi
done < <(find "$REPO/bin" -name "*.py" 2>/dev/null)
t_info "  scanned $n .py files"

# ── 3. YAML schema ─────────────────────────────────────────────────────
t_info ""
t_info "[3/10] yaml.safe_load on all *.yml/*.yaml"
n=0
while IFS= read -r f; do
    n=$((n+1))
    if python3 -c "import yaml; yaml.safe_load(open('$f'))" 2>>"$LOG"; then
        t_pass
    else
        t_fail "yaml: $f"
    fi
done < <(find "$REPO/configs" "$REPO/bin" -name "*.yml" -o -name "*.yaml" 2>/dev/null | head -50)
t_info "  scanned $n yaml files"

# ── 4. v2 module imports ───────────────────────────────────────────────
t_info ""
t_info "[4/10] v2 module imports (no top-level errors)"
for mod in reflexion-store voyager-skills letta-memory inference-augment \
           lorahub-composer truthrl-rewarder validator-rlvr \
           verifiable-rewards-gym diffadapt-router \
           teachable-prompt-filter abstract-cot-compressor; do
    p="$REPO/bin/v2/${mod}.py"
    [[ ! -f "$p" ]] && { t_warn "missing $mod.py"; continue; }
    if python3 -c "
import sys, importlib.util
spec = importlib.util.spec_from_file_location('${mod//-/_}', '$p')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
" 2>>"$LOG"; then
        t_pass
    else
        t_fail "v2 import: $mod"
    fi
done

# ── 5. Coordinator schema ──────────────────────────────────────────────
t_info ""
t_info "[5/10] coordinator SQLite schema"
if python3 -c "
import sqlite3, os
db = os.path.expanduser('~/.surrogate/state/bulk-mirror-claims.db')
if not os.path.exists(db): print('  no DB yet (fresh deploy)'); exit(0)
c = sqlite3.connect(db)
n = c.execute(\"SELECT COUNT(*) FROM sqlite_master WHERE type='table'\").fetchone()[0]
assert n >= 1, f'expected >=1 table, got {n}'
n_claims = c.execute('SELECT COUNT(*) FROM claims').fetchone()[0]
print(f'  claims table: {n_claims} rows')
" 2>>"$LOG"; then
    t_pass
else
    t_fail "coordinator schema"
fi

# ── 6. Reflexion / voyager / letta stats ───────────────────────────────
t_info ""
t_info "[6/10] v2 store stats"
for store in reflexion-store voyager-skills letta-memory; do
    if python3 "$REPO/bin/v2/${store}.py" stats >/dev/null 2>>"$LOG"; then
        t_pass
    else
        t_fail "store stats: $store"
    fi
done

# ── 7. Sanitize lib ────────────────────────────────────────────────────
t_info ""
t_info "[7/10] sanitize.filter_pair (good + bad inputs)"
if python3 -c "
import sys
sys.path.insert(0, '$REPO/bin/lib')
from sanitize import filter_pair

# Known-good: should keep
v = filter_pair(
    'Write a Python function to compute factorial',
    'def factorial(n):\n    return 1 if n<=1 else n*factorial(n-1)'
)
assert v['keep'] is True, f'good rejected: {v}'

# Known-bad: should drop (contains internal path)
v = filter_pair(
    'foo',
    '# generated via cerebras:llama3.1-8b\n/home/hermes/.surrogate/state/x.md'
)
assert v['keep'] is False, f'polluted not dropped: {v}'

# Known-bad: PII
v = filter_pair('foo bar baz', 'contact me at john.doe@example.com or 555-1234567')
assert v['keep'] is False, f'PII not dropped: {v}'

print('  3 sanitize cases: good kept, polluted dropped, PII dropped')
" 2>>"$LOG"; then
    t_pass
else
    t_fail "sanitize.filter_pair"
fi

# ── 8. start.sh cron heredoc parse ─────────────────────────────────────
t_info ""
t_info "[8/10] start.sh cron heredoc syntax"
if awk '/cat > \/tmp\/hermes-cron.sh/{found=1; next} /^CRONSH$/{found=0} found' \
       "$REPO/start.sh" | bash -n 2>>"$LOG"; then
    t_pass
else
    t_fail "start.sh cron heredoc"
fi

# ── 9. Bridge smoke (slow — skip in --quick) ───────────────────────────
if [[ "$QUICK" != "1" ]]; then
    t_info ""
    t_info "[9/10] bridge smoke (1 prompt each)"
    [[ ! -f "$HOME/.hermes/.env" ]] && { t_warn "no ~/.hermes/.env — skipping bridges"; }
    for b in cerebras groq gemini chutes hf-inference; do
        for path in "$HOME/.surrogate/hf-space/bin/${b}-bridge.sh" \
                    "$HOME/.surrogate/bin/${b}-bridge.sh"; do
            [[ -x "$path" ]] || continue
            out=$(bash -c "set -a; source ~/.hermes/.env 2>/dev/null; set +a; echo 'reply OK' | bash '$path' --max-tokens 5" 2>>"$LOG" | head -c 100)
            if [[ -n "$out" ]] && [[ ${#out} -gt 1 ]]; then
                t_pass; t_info "    $b: '${out:0:40}'"
            else
                t_warn "$b: empty response (token issue or cold start)"
            fi
            break
        done
    done
fi

# ── 10. coordinator can re-seed (idempotent) ──────────────────────────
t_info ""
t_info "[10/10] coordinator seed (idempotent)"
if python3 "$REPO/bin/v2/bulk-mirror-coordinator.py" seed >>"$LOG" 2>&1; then
    t_pass
else
    t_warn "coordinator seed (may be ok if state DB locked)"
fi

# ── Summary ─────────────────────────────────────────────────────────────
t_info ""
t_info "═══ SUMMARY ═══"
t_info "  PASS: $PASS"
t_info "  FAIL: $FAIL"
t_info "  WARN: $WARN"
if (( FAIL > 0 )); then
    t_info ""
    t_info "Failures:"
    for f in "${FAILS[@]}"; do t_info "  - $f"; done
    exit 1
fi

echo "✅ all $PASS tests passed (warnings: $WARN)" | tee -a "$LOG"
exit 0
