#!/usr/bin/env bash
# Surrogate-1 — full-service monitor (every 5 min via daemon).
#
# Checks 7 domains, ~30 services total. Discord alert on any state change.
# Runs on Mac (cron via monitor-daemon.sh) and anchor (cron-loop.sh).
#
# Domains:
#   1. HF Spaces (5)
#   2. HF Datasets (5)
#   3. HF Models / LoRA (1+)
#   4. OCI compute (3 VMs)
#   5. GitHub axentx org (6 repos: dev-chain target)
#   6. LLM bridges (8 — smoke ping)
#   7. External free GPU (Lightning, Kaggle, Modal — last-job status)
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

LOG="$HOME/.surrogate/oci/logs/full-monitor.log"
STATE="$HOME/.surrogate/oci/state/full-monitor.json"
mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE")"

DISCORD="${DISCORD_WEBHOOK:-}"
HF_TOKEN_USE="${HF_TOKEN_PRO:-${HF_TOKEN:-}}"
GH_TOKEN_USE="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

# Service registries
HF_SPACES=(
    "axentx/surrogate-1"
    "ashirato/surrogate-1-shard1"
    "surrogate1/surrogate-1-shard2"
    "ashirato/surrogate-1-zero-gpu"
    "surrogate1/surrogate-1-zero-gpu"
)
HF_DATASETS=(
    "axentx/surrogate-1-training-pairs"
    "axentx/surrogate-1-pairs-A"
    "axentx/surrogate-1-pairs-B"
    "axentx/surrogate-1-pairs-C"
    "axentx/surrogate-1-pairs-D"
)
HF_MODELS=(
    "axentx/surrogate-1-coder-7b-v1"
)
# GH_REPOS dynamically discovered via /api/orgs/{org}/repos.
# Local Mac paths checked separately (dev-chain operates on local clones).
GH_ORGS=("axentx" "AXENTX")
LOCAL_PROJECTS=(
    "$HOME/axentx/Costinel"
    "$HOME/axentx/vanguard"
    "$HOME/axentx/arkship"
    "$HOME/axentx/surrogate"
    "$HOME/axentx/workio"
    "$HOME/axentx/hermes-toolbelt"
)
BRIDGES=(
    "cerebras" "groq" "gemini" "chutes" "openrouter"
    "hf-inference" "zero-gpu"
)

declare -a SUMMARY_LINES=()
TOTAL=0; OK=0; DEGRADED=0; DOWN=0

emit() {
    SUMMARY_LINES+=("$1")
    TOTAL=$((TOTAL+1))
    case "$1" in
        *"[OK]"*)        OK=$((OK+1)) ;;
        *"[DEGRADED]"*)  DEGRADED=$((DEGRADED+1)) ;;
        *"[DOWN]"*)      DOWN=$((DOWN+1)) ;;
    esac
}

# ── 1. HF Spaces ──────────────────────────────────────────────────────
echo "[$(date '+%H:%M:%S')] checking HF Spaces..." >> "$LOG"
for s in "${HF_SPACES[@]}"; do
    stage=$(curl -fsS --max-time 6 -H "Authorization: Bearer $HF_TOKEN_USE" \
        "https://huggingface.co/api/spaces/$s/runtime" 2>/dev/null \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('stage','?'))" 2>/dev/null)
    case "$stage" in
        RUNNING)         emit "Space $s [OK] $stage" ;;
        BUILDING|APP_STARTING|RUNNING_APP_STARTING) emit "Space $s [DEGRADED] $stage" ;;
        RUNTIME_ERROR|BUILD_ERROR|STOPPED) emit "Space $s [DOWN] $stage" ;;
        *)               emit "Space $s [DEGRADED] unknown=$stage" ;;
    esac
done

# ── 2. HF Datasets (size + freshness) ─────────────────────────────────
echo "[$(date '+%H:%M:%S')] checking HF Datasets..." >> "$LOG"
for d in "${HF_DATASETS[@]}"; do
    info=$(curl -fsS --max-time 6 -H "Authorization: Bearer $HF_TOKEN_USE" \
        "https://huggingface.co/api/datasets/$d" 2>/dev/null \
        | python3 -c "
import json, sys, datetime
try:
    o = json.load(sys.stdin)
    sz = o.get('usedStorage', 0) / 1024**3
    mod = o.get('lastModified', '')[:19]
    age = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(mod)).total_seconds() / 60 if mod else 99999
    print(f'{sz:.1f}|{age:.1f}')
except: print('0|99999')
" 2>/dev/null)
    sz="${info%|*}"; age="${info##*|}"
    age_int=${age%.*}
    if (( age_int < 60 )); then
        emit "Dataset $d [OK] ${sz}GB age ${age}min"
    elif (( age_int < 1440 )); then
        emit "Dataset $d [DEGRADED] ${sz}GB age ${age}min (no recent push)"
    else
        emit "Dataset $d [DOWN] ${sz}GB stale (>24hr)"
    fi
done

# ── 3. HF Models (LoRA freshness) ─────────────────────────────────────
for m in "${HF_MODELS[@]}"; do
    exists=$(curl -fsS --max-time 6 -H "Authorization: Bearer $HF_TOKEN_USE" \
        "https://huggingface.co/api/models/$m" 2>/dev/null \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print('Y' if d.get('id') else 'N')" 2>/dev/null)
    if [[ "$exists" == "Y" ]]; then
        emit "Model $m [OK] live"
    else
        emit "Model $m [DOWN] missing"
    fi
done

# ── 4. OCI compute ────────────────────────────────────────────────────
echo "[$(date '+%H:%M:%S')] checking OCI..." >> "$LOG"
COMP=$(awk -F= '/^tenancy=/{print $2}' ~/.oci/config 2>/dev/null | head -1 | tr -d ' \t')
oci_data=""
if [[ -n "$COMP" ]]; then
    oci_data=$(OCI_CLI_AUTH=security_token oci compute instance list \
        --compartment-id "$COMP" \
        --query 'data[*].{name:"display-name",state:"lifecycle-state"}' \
        --raw-output 2>/dev/null)
fi
for vm in coordinator watchdog anchor; do
    name="surrogate-$vm"
    if [[ -z "$oci_data" ]]; then
        emit "OCI $name [DEGRADED] auth-fail (session expired?)"
    else
        state=$(echo "$oci_data" | python3 -c "
import json, sys
try:
    rs = json.load(sys.stdin)
    for r in rs:
        if r.get('name') == '$name':
            print(r.get('state','?')); break
    else: print('NOT_FOUND')
except: print('parse-error')
" 2>/dev/null)
        case "$state" in
            RUNNING)         emit "OCI $name [OK] $state" ;;
            STARTING|PROVISIONING) emit "OCI $name [DEGRADED] $state" ;;
            STOPPED|TERMINATED) emit "OCI $name [DOWN] $state" ;;
            NOT_FOUND)       emit "OCI $name [DOWN] not provisioned" ;;
            *)               emit "OCI $name [DEGRADED] $state" ;;
        esac
    fi
done

# ── 5. GitHub axentx org (dynamic discovery + local clone freshness) ──
echo "[$(date '+%H:%M:%S')] checking GitHub..." >> "$LOG"
GH_REPOS_DYN=()
if [[ -n "$GH_TOKEN_USE" ]]; then
    for org in "${GH_ORGS[@]}"; do
        repos=$(curl -fsS --max-time 8 \
            -H "Authorization: Bearer $GH_TOKEN_USE" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/orgs/$org/repos?per_page=50&type=all" 2>/dev/null \
            | python3 -c "
import json, sys
try:
    rs = json.load(sys.stdin)
    if isinstance(rs, list):
        for r in rs:
            n = r.get('full_name', '')
            p = r.get('pushed_at','')[:19]
            if n: print(f'{n}|{p}')
except: pass
" 2>/dev/null)
        while IFS='|' read -r repo push; do
            [[ -z "$repo" ]] && continue
            GH_REPOS_DYN+=("$repo|$push")
        done <<< "$repos"
    done
fi

if (( ${#GH_REPOS_DYN[@]} == 0 )); then
    emit "GitHub axentx-org [DEGRADED] 0 repos discovered (token? org private?)"
else
    for entry in "${GH_REPOS_DYN[@]}"; do
        repo="${entry%|*}"; push="${entry##*|}"
        if [[ -z "$push" || "$push" == "None" ]]; then
            emit "GitHub $repo [DEGRADED] never pushed"
            continue
        fi
        age_h=$(python3 -c "
import datetime
try: print(int((datetime.datetime.utcnow() - datetime.datetime.fromisoformat('$push')).total_seconds()/3600))
except: print(99999)
" 2>/dev/null)
        if (( age_h < 24 )); then
            emit "GitHub $repo [OK] pushed ${age_h}h ago"
        elif (( age_h < 168 )); then
            emit "GitHub $repo [DEGRADED] pushed ${age_h}h (~$((age_h/24))d) ago"
        else
            emit "GitHub $repo [DEGRADED] pushed ${age_h}h ago (>1wk, dev-chain stale)"
        fi
    done
fi

# Local Mac clones (dev-chain target paths)
echo "[$(date '+%H:%M:%S')] checking local axentx projects..." >> "$LOG"
for p in "${LOCAL_PROJECTS[@]}"; do
    name=$(basename "$p")
    if [[ ! -d "$p" ]]; then
        emit "Local axentx/$name [DEGRADED] path missing on Mac"
        continue
    fi
    # If git repo, check last commit age
    if [[ -d "$p/.git" ]]; then
        last=$(git -C "$p" log -1 --format=%ct 2>/dev/null || echo 0)
        if [[ "$last" == "0" ]]; then
            emit "Local axentx/$name [DEGRADED] no commits"
        else
            age_h=$(( ($(date +%s) - last) / 3600 ))
            if (( age_h < 24 )); then
                emit "Local axentx/$name [OK] last commit ${age_h}h ago"
            else
                emit "Local axentx/$name [DEGRADED] last commit ${age_h}h ago"
            fi
        fi
    else
        emit "Local axentx/$name [DEGRADED] not a git repo"
    fi
done

# ── 6. LLM bridges (smoke ping — local only, only if .env loaded) ─────
if [[ -f "$HOME/.hermes/.env" ]]; then
    echo "[$(date '+%H:%M:%S')] checking bridges..." >> "$LOG"
    for b in "${BRIDGES[@]}"; do
        path="$HOME/.surrogate/hf-space/bin/${b}-bridge.sh"
        [[ ! -x "$path" ]] && path="$HOME/.surrogate/bin/${b}-bridge.sh"
        if [[ ! -x "$path" ]]; then
            emit "Bridge $b [DEGRADED] missing"
            continue
        fi
        out=$(echo "say OK only" | bash "$path" --max-tokens 5 2>/dev/null | head -c 50)
        if [[ -n "$out" ]] && [[ ${#out} -ge 1 ]]; then
            emit "Bridge $b [OK] ok"
        else
            emit "Bridge $b [DEGRADED] no response"
        fi
    done
fi

# ── 7. External free GPU services ─────────────────────────────────────
# Light check: presence of recent training-job log file = recent activity
for svc_log in lightning-trainer.log kaggle-trainer.log; do
    p="$HOME/.surrogate/logs/$svc_log"
    name="${svc_log%-trainer.log}"
    if [[ -f "$p" ]]; then
        age_min=$(( ($(date +%s) - $(stat -f %m "$p" 2>/dev/null || stat -c %Y "$p" 2>/dev/null || echo 0)) / 60 ))
        if (( age_min < 360 )); then
            emit "ExtGPU $name [OK] log ${age_min}min ago"
        elif (( age_min < 1440 )); then
            emit "ExtGPU $name [DEGRADED] log ${age_min}min ago"
        else
            emit "ExtGPU $name [DOWN] log >24h stale"
        fi
    else
        emit "ExtGPU $name [DEGRADED] no log yet"
    fi
done

# ── Render summary ─────────────────────────────────────────────────────
{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] full-service-monitor"
    echo "  totals: TOTAL=$TOTAL OK=$OK DEGRADED=$DEGRADED DOWN=$DOWN"
    for ln in "${SUMMARY_LINES[@]}"; do echo "  $ln"; done
} >> "$LOG"

# ── State diff vs previous tick → notify on changes ───────────────────
NEW="$TOTAL,$OK,$DEGRADED,$DOWN"
LAST=$(jq -r '.summary // ""' "$STATE" 2>/dev/null || echo "")
if [[ "$NEW" != "$LAST" ]]; then
    HIGH_PRIO=""
    for ln in "${SUMMARY_LINES[@]}"; do
        [[ "$ln" == *"[DOWN]"* ]] && HIGH_PRIO="${HIGH_PRIO}\\n• ${ln}"
    done
    if [[ -n "$DISCORD" ]] && (( DOWN > 0 || DEGRADED > 5 )); then
        msg="🔍 full-monitor: TOTAL=$TOTAL OK=$OK DEGRADED=$DEGRADED **DOWN=$DOWN**${HIGH_PRIO}"
        curl -s -X POST -H "Content-Type: application/json" \
            -d "{\"content\":\"$msg\"}" "$DISCORD" >/dev/null 2>&1 || true
    fi
    {
        echo "{"
        echo "  \"summary\": \"$NEW\","
        echo "  \"ts\": $(date +%s),"
        echo "  \"total\": $TOTAL,"
        echo "  \"ok\": $OK,"
        echo "  \"degraded\": $DEGRADED,"
        echo "  \"down\": $DOWN"
        echo "}"
    } > "$STATE"
fi

# Concise stdout summary
echo "[$(date '+%H:%M:%S')] $TOTAL services: OK=$OK DEGRADED=$DEGRADED DOWN=$DOWN"
exit $(( DOWN > 0 ? 1 : 0 ))
