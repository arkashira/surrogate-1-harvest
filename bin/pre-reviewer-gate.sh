#!/usr/bin/env bash
# Lint + typecheck gate. Runs BEFORE reviewer-agent ever sees a diff so
# the reviewer never wastes attempts on lint errors.
#
# Usage: pre-reviewer-gate.sh <repo-path>
# Exit:
#   0 — all green (lint + types clean)
#   1 — any tool failed (output captured to stdout/stderr)
#   2 — repo path missing or unsupported project
#
# Detection:
#   Python   = pyproject.toml OR setup.py OR requirements.txt OR *.py top-level
#              tools: ruff (lint), mypy (types). Skips silently if not installed.
#   TypeScript/JS = package.json with "lint" and/or "typecheck" scripts
#              tools: npm run lint, npm run typecheck (or tsc --noEmit)
#   Mixed    = run both
#
# Designed to fail closed: any tool returning non-zero fails the gate.
set -uo pipefail

REPO_PATH="${1:-}"
if [[ -z "$REPO_PATH" || ! -d "$REPO_PATH" ]]; then
  echo "usage: $0 <repo-path>" >&2
  echo "       (path '$REPO_PATH' is not a directory)" >&2
  exit 2
fi

cd "$REPO_PATH" || exit 2

GATE_FAILED=0
RAN_ANYTHING=0

green()  { printf '\033[32m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

run_gate() {
  local label="$1"; shift
  RAN_ANYTHING=1
  echo "── $label ──"
  if "$@"; then
    green "  $label PASS"
  else
    red "  $label FAIL (exit $?)"
    GATE_FAILED=1
  fi
}

is_python_project() {
  [[ -f pyproject.toml || -f setup.py || -f setup.cfg || -f requirements.txt ]] && return 0
  # Fallback: any *.py at repo root
  compgen -G "*.py" > /dev/null 2>&1 && return 0
  return 1
}

is_node_project() {
  [[ -f package.json ]]
}

# ── Python gates ────────────────────────────────────────────────────────────
if is_python_project; then
  echo "::: Python project detected"

  if command -v ruff >/dev/null 2>&1; then
    run_gate "ruff" ruff check .
  else
    yellow "  ruff not installed — install with: pip install ruff"
  fi

  if command -v mypy >/dev/null 2>&1; then
    # --no-error-summary keeps output clean when invoked from agent harness
    if [[ -f mypy.ini || -f pyproject.toml || -f setup.cfg ]]; then
      run_gate "mypy" mypy --no-error-summary .
    else
      run_gate "mypy" mypy --no-error-summary --ignore-missing-imports .
    fi
  else
    yellow "  mypy not installed — install with: pip install mypy"
  fi
fi

# ── TypeScript / JavaScript gates ──────────────────────────────────────────
if is_node_project; then
  echo "::: Node project detected"

  HAS_LINT_SCRIPT=$(node -e "try { const p=require('./package.json'); process.stdout.write(p.scripts && p.scripts.lint ? '1' : '0') } catch { process.stdout.write('0') }" 2>/dev/null || echo 0)
  HAS_TYPECHECK_SCRIPT=$(node -e "try { const p=require('./package.json'); process.stdout.write(p.scripts && p.scripts.typecheck ? '1' : '0') } catch { process.stdout.write('0') }" 2>/dev/null || echo 0)

  if [[ "$HAS_LINT_SCRIPT" == "1" ]]; then
    run_gate "npm run lint" npm run --silent lint
  elif command -v eslint >/dev/null 2>&1; then
    run_gate "eslint" eslint --max-warnings 0 .
  else
    yellow "  no lint script and eslint not on PATH"
  fi

  if [[ "$HAS_TYPECHECK_SCRIPT" == "1" ]]; then
    run_gate "npm run typecheck" npm run --silent typecheck
  elif [[ -f tsconfig.json ]] && command -v npx >/dev/null 2>&1; then
    run_gate "tsc --noEmit" npx --no-install tsc --noEmit
  else
    yellow "  no typecheck script and no tsconfig.json"
  fi
fi

if [[ "$RAN_ANYTHING" == "0" ]]; then
  yellow "no Python or Node project markers — skipping (exit 0)"
  exit 0
fi

if [[ "$GATE_FAILED" == "1" ]]; then
  red "GATE FAILED — fix lint/types before reviewer can run"
  exit 1
fi

green "GATE PASSED — reviewer can run"
exit 0
