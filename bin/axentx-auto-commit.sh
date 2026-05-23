#!/usr/bin/env bash
# Auto-commit Hermes's work in axentx projects.
# Runs after each cron cycle to preserve writes that Team Lead didn't commit itself.
# Only commits REAL code changes (skips noise like test_output, data/, node_modules).

set -u
LOG="/opt/surrogate-1-harvest/logs/axentx-dev-loop.log"
AXENTX="/opt/axentx"
PROJECTS=(Costinel Vanguard arkship surrogate workio)

# Paths we skip (not user code)
SKIP_PATTERNS=(
    "test_output/"
    "data/raw/"
    "data/training-jsonl/"
    "node_modules/"
    ".venv/"
    "*.log"
    "__pycache__/"
    ".DS_Store"
)

for proj in "${PROJECTS[@]}"; do
    PP="$AXENTX/$proj"
    [[ -d "$PP/.git" ]] || continue

    # Check if there are changes
    if ! git -C "$PP" diff --quiet HEAD 2>/dev/null || [[ -n "$(git -C "$PP" ls-files --others --exclude-standard 2>/dev/null)" ]]; then
        # Build list of files to add (excluding noise)
        CHANGED=$(git -C "$PP" status --porcelain 2>/dev/null | awk '{print $2}')
        TO_ADD=()
        for f in $CHANGED; do
            skip=0
            for pat in "${SKIP_PATTERNS[@]}"; do
                [[ "$f" == *"$pat"* ]] && { skip=1; break; }
            done
            [[ $skip -eq 0 ]] && TO_ADD+=("$f")
        done

        if [[ ${#TO_ADD[@]} -gt 0 ]]; then
            # Commit with Hermes author signature
            git -C "$PP" add "${TO_ADD[@]}" 2>/dev/null
            # Only commit if staged changes exist
            if ! git -C "$PP" diff --cached --quiet 2>/dev/null; then
                FILES_STR=$(printf "%s, " "${TO_ADD[@]:0:3}" | sed 's/, $//')
                [[ ${#TO_ADD[@]} -gt 3 ]] && FILES_STR="$FILES_STR +$((${#TO_ADD[@]} - 3))"
                MSG="chore(hermes): auto-commit cron cycle work ($FILES_STR)"
                git -C "$PP" commit -m "$MSG" \
                    --author="Hermes Agent <hermes@ashira.local>" \
                    >> "$LOG.commit" 2>&1
                if [[ $? -eq 0 ]]; then
                    echo "[$(date '+%H:%M')] ✓ committed $proj: ${#TO_ADD[@]} files" | tee -a "$LOG"
                fi
            fi
        fi
    fi
done
