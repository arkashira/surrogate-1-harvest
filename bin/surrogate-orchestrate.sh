#!/usr/bin/env bash
# Auto-Dev orchestration — chains Hermes team agents like Claude Code's Agent tool
# Flow: architect → dev → qa → reviewer (optional ops for infra tasks)
# Each stage produces artifact → feeds into next
#
# Usage:
#   surrogate-orchestrate.sh "task description"
#   surrogate-orchestrate.sh --mode plan "task"     # architect only
#   surrogate-orchestrate.sh --mode yolo "task"     # full chain, no gates
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

MODE="auto"
TASK=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        *) TASK="$*"; break ;;
    esac
done
[[ -z "$TASK" ]] && { echo "need task"; exit 2; }

# Colors
R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'
CY=$'\033[36m'; GR=$'\033[32m'; YE=$'\033[33m'; MA=$'\033[35m'; RE=$'\033[31m'; GY=$'\033[90m'
BCY=$'\033[96m'

SESSION_ID=$(date +%s | tail -c 9)
WORKDIR="$HOME/.claude/state/orchestrate/$SESSION_ID"
mkdir -p "$WORKDIR"

echo "${BCY}${B}╭─ Auto-Dev Orchestration ─────────────────╮${R}"
echo "${BCY}${B}│${R} session: ${YE}$SESSION_ID${R}  mode: ${MA}$MODE${R}"
echo "${BCY}${B}│${R} cwd: ${D}$(pwd)${R}"
echo "${BCY}${B}╰──────────────────────────────────────────╯${R}"
echo "${B}▸ Task:${R} $TASK"
echo ""

# Helper: call surrogate agent with specific role + feed artifacts
call_agent() {
    local role="$1" prompt="$2" output_file="$3"
    echo "${CY}▶${R} ${B}$role${R} ${D}working...${R}"
    # Use surrogate CLI to run the role-based task
    local agent_prompt="[ROLE: $role]
$prompt

Output your work to $output_file using the \`write\` tool when done.
Previous artifacts available in: $WORKDIR/
CWD: $(pwd)"
    ~/.claude/bin/surrogate -p "$agent_prompt" 2>&1 | head -50 | sed 's/^/  /'
    # Check if file written
    if [[ -f "$output_file" ]]; then
        echo "${GR}  ⎿ $role done → $(basename "$output_file") ($(wc -c < "$output_file") bytes)${R}"
        return 0
    else
        echo "${RE}  ⎿ $role: no output file written${R}"
        return 1
    fi
}

# Read project PRD if exists (DDD/TDD/architecture context)
PRD_CONTEXT=""
for prd_file in "$(pwd)/surrogate.md" "$(pwd)/SURROGATE.md"; do
    [[ -f "$prd_file" ]] && PRD_CONTEXT=$(head -c 4000 "$prd_file") && break
done
[[ -n "$PRD_CONTEXT" ]] && PRD_CONTEXT="

=== Project PRD (surrogate.md) ===
$PRD_CONTEXT
=== End PRD ==="

# ═══ Stage 1: SOLUTION ARCHITECT (SA) — high-level design ═══
SA_OUT="$WORKDIR/0-sa-design.md"
echo ""
echo "${MA}${B}═══ Stage 1/6: SOLUTION ARCHITECT${R} ${D}— DDD + design patterns${R}"
call_agent "solution-architect" "
You are a senior Solution Architect. For this task, produce a high-level technical design BEFORE any code.

Required output:
1. **Bounded contexts** (DDD) — which subdomain(s) does this touch?
2. **Domain model changes** — entities, aggregates, value objects, repositories
3. **Design patterns** to apply (Repository, Factory, Strategy, Observer, Builder, etc.) — pick deliberately, justify each
4. **Architecture style** alignment (hexagonal/MVC/MVVM/clean) — show layer flow
5. **Integration points** — APIs, events, side-effects (with sequence diagram in mermaid if non-trivial)
6. **Non-functional impacts** — perf, security, scalability, observability
7. **Risks + mitigations**

Be specific. No generic platitudes. Use codebase via read/grep/glob.
${PRD_CONTEXT}
Task: $TASK
" "$SA_OUT"

# ═══ Stage 2: ARCHITECT — file-level decomposition ═══
ARCH_OUT="$WORKDIR/1-architect-plan.md"
echo ""
echo "${MA}${B}═══ Stage 2/6: ARCHITECT${R} ${D}— file-level plan${R}"
call_agent "architect" "
You are the Tech Architect. Take the SA design and produce a CONCRETE file-level execution plan.

SA design at: $SA_OUT

Required output:
1. **Files to create/modify** — exact paths + one-line purpose each
2. **Function signatures** — for new public APIs (with types)
3. **Test files first** (TDD) — list test cases BEFORE implementation files
4. **Dependencies** — new packages? versions?
5. **Migration plan** — DB schema changes, config rollout
6. **Rollback** — how to undo if production breaks

Use existing codebase patterns — read 3-5 similar files first via \`read\`/\`grep\`.
Task: $TASK
" "$ARCH_OUT"

if [[ "$MODE" == "plan" ]]; then
    echo ""
    echo "${B}▸ Plan-only mode — stopping after architect${R}"
    [[ -f "$ARCH_OUT" ]] && cat "$ARCH_OUT"
    exit 0
fi

# ═══ Stage 3: QA-FIRST (TDD) — write tests BEFORE code ═══
TDD_OUT="$WORKDIR/2-qa-tdd-tests.md"
echo ""
echo "${MA}${B}═══ Stage 3/6: QA-FIRST (TDD)${R} ${D}— write failing tests first${R}"
call_agent "qa" "
You are the QA Engineer practicing TDD. Write FAILING tests BEFORE the dev writes any code.

SA design: $SA_OUT
Architect plan: $ARCH_OUT

Required:
1. Read existing test patterns in repo (pytest / jest / go test) via \`read\`/\`grep\`
2. Use the architect's listed test file paths
3. Write tests using \`write\` tool — they MUST fail (red phase of TDD)
4. One assertion per test, factory functions for fixtures, descriptive names
5. Cover: happy path, edge cases, error paths, security boundaries
6. NO implementation — only tests

Output: list of test file paths created + brief 'tests will fail because <reason>'
Task: $TASK
" "$TDD_OUT"

# ═══ Stage 4: DEV — implement to make tests pass ═══
DEV_OUT="$WORKDIR/3-dev-summary.md"
echo ""
echo "${MA}${B}═══ Stage 4/6: DEV${R} ${D}— implement to green${R}"
call_agent "dev" "
You are the Senior Developer. Make the QA tests PASS by implementing per the Architect plan.

SA design:    $SA_OUT
Architect:    $ARCH_OUT
QA tests:     $TDD_OUT

Strict rules:
1. Implement ONLY what's needed to make tests pass (red → green → refactor)
2. Apply DDD: Repository pattern for data access, no business logic in handlers
3. Apply design patterns from SA design (Strategy/Factory/Observer/etc.)
4. Type-strict (TS strict / Python type hints / Go generics)
5. Result/Either pattern over throws for expected errors
6. Intent-revealing names — verbs for functions, units for numerics
7. NO commented-out code, NO TODO without ticket ID, NO hallucinated imports
8. After each file: refactor for readability while keeping tests green

Use \`write\`/\`edit\` tools — write actual files, not pseudocode.
After done: write summary to output file with file list + test pass status.
Task: $TASK
" "$DEV_OUT"

# ═══ Stage 5: QA-VERIFY — run all tests + add missing coverage ═══
QA_OUT="$WORKDIR/4-qa-report.md"
echo ""
echo "${MA}${B}═══ Stage 5/6: QA-VERIFY${R} ${D}— green tests + coverage${R}"
call_agent "qa" "
You are the QA Engineer in verification phase. The dev claims tests pass — VERIFY.

QA tests written: $TDD_OUT
Dev summary:      $DEV_OUT

Required:
1. Run the test suite via \`bash\` (pytest / npm test / go test ./...)
2. Verify all tests pass (no skips, no x's)
3. Check coverage — if missing branches, add MORE tests + re-run
4. Run linting (ruff / eslint / golangci-lint) and type-check (mypy / tsc / go vet)
5. Manual sanity test of happy path

Output to file: pass/fail per check + coverage % + new tests added (if any).
Task: $TASK
" "$QA_OUT"

# ═══ Stage 4: OPS (if task mentions infra) ═══
if echo "$TASK" | grep -iqE "deploy|docker|helm|k8s|terraform|cicd|ci/cd"; then
    OPS_OUT="$WORKDIR/4-ops-checklist.md"
    echo ""
    echo "${MA}${B}═══ Stage 6a/6: OPS${R} ${D}— deploy + infra${R}"
    call_agent "ops" "
Review infrastructure aspects. Check:
- Dockerfile / helm chart / terraform validity
- Secrets / env var handling
- Resource limits
- Observability (metrics/logs/traces)

Dev summary: $DEV_OUT
Output to: $OPS_OUT
Task: $TASK
" "$OPS_OUT"
else
    echo ""
    echo "${GY}═══ Stage 6a/6: OPS — skipped (not infra task)${R}"
fi

# ═══ Stage 5: REVIEWER ═══
REVIEW_OUT="$WORKDIR/5-review-verdict.md"
echo ""
echo "${MA}${B}═══ Stage 6/6: REVIEWER${R} ${D}— final gate${R}"
call_agent "reviewer" "
FINAL REVIEW GATE. Check all prior stages:
- Architect plan: $ARCH_OUT
- Dev implementation summary: $DEV_OUT
- QA report: $QA_OUT

Judge the work on:
1. Correctness vs requirements
2. Code quality (naming, no hallucinated imports, error handling)
3. Security (no leaked secrets, input validation)
4. Tests coverage
5. Match existing codebase style

Verdict: APPROVE / REWORK / REJECT
If REWORK — specify what to redo.

Output verdict + reasons to: $REVIEW_OUT
Task: $TASK
" "$REVIEW_OUT"

# ═══ Summary ═══
echo ""
echo "${BCY}${B}╭─ Session Complete ───────────────────────╮${R}"
echo "${BCY}${B}│${R} session: $SESSION_ID"
echo "${BCY}${B}│${R} artifacts: $WORKDIR/"
echo "${BCY}${B}╰──────────────────────────────────────────╯${R}"
ls -la "$WORKDIR/" 2>&1 | tail -n +2 | awk '{print "  " $9}' | grep -v '^  $'

# Show verdict + auto-commit if APPROVED
VERDICT_TEXT=""
if [[ -f "$REVIEW_OUT" ]]; then
    VERDICT_TEXT=$(grep -iE "verdict|APPROVE|REWORK|REJECT" "$REVIEW_OUT" | head -3)
    echo ""
    echo "${B}▸ Final verdict:${R}"
    echo "$VERDICT_TEXT" | sed 's/^/  /'
fi

# Auto-commit when reviewer approves (ship code)
if echo "$VERDICT_TEXT" | grep -qi "APPROVE"; then
    echo ""
    echo "${GR}${B}▸ Reviewer approved — committing changes${R}"
    # Only commit if there are staged/unstaged changes
    if ! git -C "$(pwd)" diff --quiet 2>/dev/null || ! git -C "$(pwd)" diff --cached --quiet 2>/dev/null; then
        # Stage all changes in CWD
        git -C "$(pwd)" add -A 2>/dev/null
        # Build commit message from task + session
        COMMIT_MSG="feat: $(echo "$TASK" | head -c 72)

[surrogate auto-dev session $SESSION_ID]
[reviewed: APPROVE]"
        if git -C "$(pwd)" commit -m "$COMMIT_MSG" 2>&1 | tee -a "$WORKDIR/git-commit.log" | grep -q "master\|main\|\["; then
            COMMIT_HASH=$(git -C "$(pwd)" rev-parse --short HEAD 2>/dev/null)
            echo "${GR}  ✅ Committed: $COMMIT_HASH${R}"
        else
            echo "${YE}  ⚠ Nothing to commit (files already clean)${R}"
        fi
    else
        echo "${GY}  ○ No file changes to commit${R}"
    fi
elif echo "$VERDICT_TEXT" | grep -qi "REWORK"; then
    echo ""
    echo "${YE}${B}▸ Reviewer requested REWORK — re-running dev stage${R}"
    REWORK_NOTES=$(grep -A5 -i "REWORK" "$REVIEW_OUT" | head -8)
    DEV_OUT2="$WORKDIR/2b-dev-rework.md"
    call_agent "dev" "
REWORK requested by reviewer. Fix the following issues:

$REWORK_NOTES

Original task: $TASK
Original implementation: $DEV_OUT
QA report: $QA_OUT

Fix the issues and write updated summary to output file.
" "$DEV_OUT2"
    echo "${D}  Rework complete — re-run $0 to go through QA + review again if needed${R}"
fi
