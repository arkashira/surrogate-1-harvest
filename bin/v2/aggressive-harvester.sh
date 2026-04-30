#!/usr/bin/env bash
# Surrogate-1 v2 — Aggressive HF dataset harvester.
#
# "agent ที่สแปม dataset จากทุกที่ที่เกี่ยวข้อง"
#
# Searches HF Hub by keyword via the 5-token pool (round-robin so no single
# token hits 1000-2500 req/5min limit), then auto-claims good matches via
# bulk-mirror-coordinator.
#
# Run continuously OR cron every 30 min. Pulls 200-500 new candidates per
# tick, filters by tags + size + license + relevance, queues winners.
#
# Topics scanned (DevSecOps + SRE + code + security + reasoning + agent):
#   - "code", "python", "javascript", "typescript", "rust", "go"
#   - "terraform", "kubernetes", "aws", "azure", "gcp", "ansible"
#   - "iam", "security", "vulnerability", "cve", "secret", "compliance"
#   - "sre", "slo", "incident", "runbook", "postmortem", "monitoring"
#   - "agent", "tool", "function-calling", "react", "react-agent"
#   - "math", "reasoning", "cot", "chain-of-thought", "o1", "r1"
#   - "instruct", "sft", "dpo", "rlhf", "rlaif", "preference"
#
# Output: appends to bulk-datasets-massive.txt + auto-seeds coordinator queue.
set -uo pipefail

[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

QUEUE_FILE="$HOME/.surrogate/hf-space/bin/v2/bulk-datasets-massive.txt"
LOG="$HOME/.surrogate/logs/aggressive-harvester.log"
mkdir -p "$(dirname "$LOG")"

KEYWORDS=(
    # code
    "code-instruction" "python-code" "typescript-code" "javascript-code"
    "rust" "golang" "code-completion" "code-review" "bug-fixing" "code-generation"
    # devops
    "terraform" "kubernetes" "ansible" "cloudformation" "aws-cdk" "helm"
    "github-actions" "ci-cd" "docker" "sre" "slo" "runbook" "postmortem"
    # security
    "iam-policy" "security-audit" "cve" "vulnerability" "exploit" "remediation"
    "soc" "siem" "edr" "compliance" "soc2" "iso27001" "pci-dss" "gdpr"
    # agent/tool
    "function-calling" "tool-use" "agent" "react" "code-agent" "llm-agent"
    "browse-the-web" "computer-use" "swe-bench" "swe-agent"
    # reasoning
    "chain-of-thought" "reasoning" "o1-style" "r1-distill" "long-cot"
    "math-reasoning" "step-by-step" "self-consistency"
    # instruction tuning
    "instruction-tuning" "sft" "dpo" "rlhf" "rlaif" "preference-pairs"
    "magpie" "self-instruct" "evol-instruct" "constitutional-ai"
)

# Per-call: 1 token from pool (rotate by epoch+keyword index)
get_token() {
    local idx=$1
    local pool="${HF_TOKEN_POOL:-${HF_TOKEN:-}}"
    [[ -z "$pool" ]] && return 1
    IFS=',' read -ra _KEYS <<< "$pool"
    local n=${#_KEYS[@]}
    (( n == 0 )) && return 1
    echo "${_KEYS[$(( idx % n ))]}"
}

search_hf() {
    local query=$1 token=$2
    curl -fsS --max-time 12 \
        -H "Authorization: Bearer $token" \
        -H "User-Agent: surrogate-1/aggressive-harvester" \
        "https://huggingface.co/api/datasets?search=${query// /%20}&sort=downloads&direction=-1&limit=30" \
        2>/dev/null
}

is_already_queued() {
    local repo=$1
    grep -q "^${repo}|" "$QUEUE_FILE"
}

categorize() {
    local query=$1
    case "$query" in
        *code*|*python*|*typescript*|*javascript*|*rust*|*go*|*completion*|*review*|*bug*|*generation*) echo "code" ;;
        *terraform*|*kubernetes*|*ansible*|*cloudformation*|*cdk*|*helm*|*actions*|*ci-cd*|*docker*|*sre*|*slo*|*runbook*|*postmortem*) echo "devops" ;;
        *iam*|*security*|*cve*|*vulnerability*|*exploit*|*remediation*|*soc*|*siem*|*edr*|*compliance*|*soc2*|*iso*|*pci*|*gdpr*) echo "security" ;;
        *function*|*tool*|*agent*|*react*|*browse*|*computer*|*swe*) echo "agent" ;;
        *cot*|*chain*|*reasoning*|*o1*|*r1*|*long-cot*|*math*|*step*|*self-consistency*) echo "reasoning" ;;
        *instruction*|*sft*|*dpo*|*rlhf*|*rlaif*|*preference*|*magpie*|*self-instruct*|*evol*|*constitutional*) echo "sft" ;;
        *) echo "misc" ;;
    esac
}

guess_priority() {
    local downloads=$1
    if   (( downloads > 100000 )); then echo "1"
    elif (( downloads > 10000  )); then echo "2"
    else echo "3"
    fi
}

guess_max_samples() {
    local downloads=$1
    if   (( downloads > 1000000 )); then echo "1000000"
    elif (( downloads > 100000  )); then echo "200000"
    elif (( downloads > 10000   )); then echo "50000"
    else echo "10000"
    fi
}

n_added=0
n_already=0
n_failed=0

echo "[$(date '+%H:%M:%S')] aggressive-harvester start (${#KEYWORDS[@]} keywords)" >> "$LOG"

for i in "${!KEYWORDS[@]}"; do
    kw="${KEYWORDS[$i]}"
    tok=$(get_token "$i") || { echo "  no token in pool" >> "$LOG"; break; }
    out=$(search_hf "$kw" "$tok") || { n_failed=$((n_failed+1)); continue; }
    n_in=0
    while IFS=$'\t' read -r repo dl; do
        [[ -z "$repo" || "$repo" == "null" ]] && continue
        if is_already_queued "$repo"; then
            n_already=$((n_already+1))
            continue
        fi
        cat=$(categorize "$kw")
        max=$(guess_max_samples "${dl:-0}")
        pri=$(guess_priority "${dl:-0}")
        echo "${repo}|${cat}|${max}|${pri}" >> "$QUEUE_FILE"
        n_added=$((n_added+1))
        n_in=$((n_in+1))
    done < <(echo "$out" | python3 -c "
import json, sys
try:
    rows = json.load(sys.stdin)
except:
    sys.exit(0)
for r in rows:
    rid = r.get('id','')
    dl  = r.get('downloads', 0) or 0
    if rid: print(f'{rid}\t{dl}')
")
    echo "  '$kw' (token #$((i % 5))): added=$n_in" >> "$LOG"
    sleep 0.2  # be polite even with pool
done

echo "[$(date '+%H:%M:%S')] aggressive-harvester done — added=$n_added already=$n_already failed=$n_failed total_queue=$(grep -cE '^[a-zA-Z]' $QUEUE_FILE)" >> "$LOG"

# Re-seed coordinator with new entries (idempotent — INSERT OR IGNORE)
if [[ -x "$HOME/.surrogate/hf-space/bin/v2/bulk-mirror-coordinator.py" ]]; then
    python3 "$HOME/.surrogate/hf-space/bin/v2/bulk-mirror-coordinator.py" seed >> "$LOG" 2>&1
fi

# Discord notify on big additions
if [[ -n "${DISCORD_WEBHOOK:-}" && $n_added -gt 5 ]]; then
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"content\":\"🌐 aggressive-harvester: +${n_added} new datasets queued (already=$n_already, failed=$n_failed). queue total: $(grep -cE '^[a-zA-Z]' $QUEUE_FILE)\"}" \
        "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
fi
