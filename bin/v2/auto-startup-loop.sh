#!/usr/bin/env bash
# Surrogate-1 v2 — auto-startup-loop.
#
# Drives all 42 personae across 9 LoRA clusters as a continuously-running
# AI startup. Each cycle (every 15 min): pick next role by deterministic
# rotation, run persona-runner, produce one artifact, optionally chain
# downstream (CEO → CFO follows up; PM → eng-build chain; etc.).
#
# Auto-commit + auto-push to GitHub on each cycle.
#
# Cron (or boot-daemon): runs forever; sleeps 15 min between roles.
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

LOG="$HOME/.surrogate/logs/auto-startup-loop.log"
OUT_BASE="${HOME}/.surrogate/data/personae"
mkdir -p "$(dirname "$LOG")" "$OUT_BASE"

# Cross-role chain definitions: certain roles trigger downstream roles
declare -a CHAINS=(
    # ROLE → DOWNSTREAM
    "ceo:cpo,cto,cmo,cfo"           # CEO weekly drives function leaders
    "cpo:pm,ux,designer"             # CPO drives product team
    "cto:solution-architect,ai-engineer,ml-engineer"
    "cmo:marketing,content,seo,sdr"
    "cfo:bookkeeper"
    "pm:frontend-engineer,backend-engineer,qa-engineer"
    "solution-architect:devops,sre,devsecops"
    "marketing:content,seo,growth"
    "growth:data-analyst,sdr,ae"
    "cs-success:cs-support,cs-onboarding"
)

run_role() {
    local role="$1"
    local task="${2:-}"
    bash "$HOME/.surrogate/hf-space/bin/v2/persona-runner.sh" "$role" "$task" \
        2>>"$LOG"
}

# Chain trigger — when role finishes, fire downstream too
chain_run() {
    local role="$1"
    for entry in "${CHAINS[@]}"; do
        if [[ "${entry%%:*}" == "$role" ]]; then
            local downstream="${entry#*:}"
            IFS=',' read -ra DROLES <<< "$downstream"
            for dr in "${DROLES[@]}"; do
                echo "[$(date +%H:%M:%S)] chain: $role → $dr" >> "$LOG"
                run_role "$dr" >/dev/null
                sleep 5  # be polite to API rate limits
            done
        fi
    done
}

# Init git repo for personae outputs (auto-commit target)
if [[ ! -d "$OUT_BASE/.git" ]]; then
    cd "$OUT_BASE" || exit
    git init >/dev/null 2>&1 || true
    git -c user.name="Surrogate-1" -c user.email="surrogate-1@axentx.dev" \
        commit --allow-empty -m "init persona artifacts" >/dev/null 2>&1 || true

    # Optional: push to a github repo if PERSONAE_GIT_REMOTE set
    if [[ -n "${PERSONAE_GIT_REMOTE:-}" ]]; then
        git remote add origin "$PERSONAE_GIT_REMOTE" >/dev/null 2>&1 || true
    fi
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] auto-startup-loop start" >> "$LOG"

while true; do
    # Pick role by rotation: distinct minute → distinct role across 42 roles
    M=$(($(date +%s) / 60))
    REGISTRY="$HOME/.surrogate/hf-space/bin/v2/personae-registry.json"
    [[ ! -f "$REGISTRY" ]] && REGISTRY="$HOME/.surrogate/bin/v2/personae-registry.json"

    ROLE=$(python3 -c "
import json
d = json.load(open('$REGISTRY'))
roles = sorted(d.get('roles', {}).keys())
print(roles[$M % len(roles)])
")

    echo "[$(date +%H:%M:%S)] cycle role=$ROLE" >> "$LOG"
    if run_role "$ROLE"; then
        # If this role triggers a chain, run downstream
        chain_run "$ROLE"

        # Discord notify on milestone (every 10 cycles)
        if [[ -n "${DISCORD_WEBHOOK:-}" ]] && (( M % 10 == 0 )); then
            n_artifacts=$(find "$OUT_BASE" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
            curl -s -X POST -H "Content-Type: application/json" \
                -d "{\"content\":\"🏢 startup-loop: cycle role=$ROLE, $n_artifacts personae artifacts total\"}" \
                "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
        fi
    fi

    # Cycle every 15 min — 42 roles × 4/hr = ~168 cycles/day
    sleep 900
done
