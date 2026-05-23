#!/usr/bin/env bash
# Mutation testing runner — Python (mutmut) and TypeScript (stryker).
#
# Mutation testing reveals weak tests: a "killed mutant" means tests
# detected the mutation; a "survived mutant" means a bug-equivalent change
# slipped past your test suite. Higher score = stronger tests.
#
# This script is *non-blocking* by design — reports score, never fails CI.
# Adapter publish gate (#11) reads the report and decides.
#
# Usage: run-mutation-tests.sh <repo-path> [--target <package>] [--score-out <file>]
# Output: stdout summary + JSON report at .mutation-report.json (or --score-out)
set -uo pipefail

REPO_PATH="${1:-}"
TARGET=""
SCORE_OUT=""

shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)    TARGET="$2"; shift 2 ;;
    --score-out) SCORE_OUT="$2"; shift 2 ;;
    *)           echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$REPO_PATH" || ! -d "$REPO_PATH" ]]; then
  echo "usage: $0 <repo-path> [--target <package>] [--score-out <file>]" >&2
  exit 2
fi

cd "$REPO_PATH" || exit 2

SCORE_OUT="${SCORE_OUT:-$REPO_PATH/.mutation-report.json}"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SCORE=""
TOOL=""
KILLED=""
SURVIVED=""

run_mutmut() {
  TOOL="mutmut"
  echo "::: running mutmut on ${TARGET:-<auto-discover>}"
  if [[ -n "$TARGET" ]]; then
    mutmut run --paths-to-mutate "$TARGET" || true
  else
    mutmut run || true
  fi
  # mutmut results: parse `mutmut results` output for killed/survived counts.
  local results
  results="$(mutmut results 2>/dev/null || echo '')"
  KILLED=$(echo "$results" | grep -E "killed|✓"  | wc -l | tr -d ' ')
  SURVIVED=$(echo "$results" | grep -E "survived" | wc -l | tr -d ' ')
  local total=$((KILLED + SURVIVED))
  if [[ "$total" -gt 0 ]]; then
    SCORE=$(python3 -c "print(round($KILLED * 100.0 / $total, 1))")
  else
    SCORE="0"
  fi
}

run_stryker() {
  TOOL="stryker"
  echo "::: running stryker on ${TARGET:-<auto-discover>}"
  if [[ -n "$TARGET" ]]; then
    npx --no-install stryker run --mutate "$TARGET/**/*.{ts,tsx}" || true
  else
    npx --no-install stryker run || true
  fi
  # Stryker writes reports/mutation/mutation.json — parse mutationScore.
  if [[ -f reports/mutation/mutation.json ]]; then
    SCORE=$(node -e "const r=require('./reports/mutation/mutation.json');process.stdout.write(String(r.mutationScore||0))" 2>/dev/null || echo "0")
    KILLED=$(node -e "const r=require('./reports/mutation/mutation.json');let n=0;for(const f of Object.values(r.files||{})){for(const m of f.mutants||[]){if(m.status==='Killed')n++;}};process.stdout.write(String(n))" 2>/dev/null || echo 0)
    SURVIVED=$(node -e "const r=require('./reports/mutation/mutation.json');let n=0;for(const f of Object.values(r.files||{})){for(const m of f.mutants||[]){if(m.status==='Survived')n++;}};process.stdout.write(String(n))" 2>/dev/null || echo 0)
  else
    SCORE="0"; KILLED=0; SURVIVED=0
  fi
}

# Detect project + tool availability.
if [[ -f pyproject.toml || -f setup.py ]] && command -v mutmut >/dev/null 2>&1; then
  run_mutmut
elif [[ -f package.json ]] && command -v npx >/dev/null 2>&1; then
  run_stryker
else
  echo "neither (Python+mutmut) nor (Node+stryker) available — skipping" >&2
  cat > "$SCORE_OUT" <<EOF
{"timestamp":"$TIMESTAMP","tool":"none","score":null,"killed":0,"survived":0,"reason":"no tooling available"}
EOF
  exit 0
fi

cat > "$SCORE_OUT" <<EOF
{"timestamp":"$TIMESTAMP","tool":"$TOOL","target":"${TARGET:-auto}","score":$SCORE,"killed":$KILLED,"survived":$SURVIVED}
EOF

echo
echo "═══ Mutation testing summary ═══"
echo "  tool:     $TOOL"
echo "  target:   ${TARGET:-auto}"
echo "  killed:   $KILLED"
echo "  survived: $SURVIVED"
echo "  score:    $SCORE%"
echo "  report:   $SCORE_OUT"
echo
echo "Score interpretation: ≥80% strong, 60-80% adequate, <60% weak."
echo "(non-blocking — adapter gate #11 reads this report)"
exit 0
