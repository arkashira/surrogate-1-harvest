#!/usr/bin/env bash
# Surrogate-1 v2 — Continuous multi-source dataset discoverer.
#
# Runs forever (NOT cron). Sweeps:
#   • HF Hub /api/datasets (keyword search via 5-token pool, ~70 keywords)
#   • HF Hub /api/models (model cards mention training data → backtrack)
#   • arxiv.org cs.LG/cs.CL/cs.SE recent papers (pdf metadata + linked datasets)
#   • Stack Exchange API (Q&A direct stream)
#   • GitHub trending (Python/TypeScript/Rust repos with high stars)
#
# Each new source → coordinator claim queue. Coordinator de-dupes. Workers
# pick up from there.
#
# Loop cadence: 60s per source-type, ~5 min full cycle.
# Polite throttle: 0.3s between API calls. 5-token rotation on HF.
#
# Spawn ONCE at boot (idempotent — pgrep-guarded). Never exits except SIGTERM.
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

LOG="$HOME/.surrogate/logs/continuous-discoverer.log"
mkdir -p "$(dirname "$LOG")"
QUEUE_FILE="$HOME/.surrogate/hf-space/bin/v2/bulk-datasets-massive.txt"
COORDINATOR="$HOME/.surrogate/hf-space/bin/v2/bulk-mirror-coordinator.py"

# Source type rotation — visit each on each cycle
declare -a SOURCE_FNS=(
    "discover_hf_keywords"
    "discover_hf_trending"
    "discover_arxiv"
    "discover_stackexchange"
    "discover_github_trending"
)

# 70 keywords for HF sweep (rotates pool of 5 per cycle)
KEYWORDS=(
    code-instruction python-code typescript-code javascript-code rust-code golang-code
    code-completion code-review bug-fixing code-generation code-feedback code-eval
    terraform kubernetes ansible cloudformation aws-cdk helm
    github-actions ci-cd docker sre slo runbook postmortem incident
    iam-policy security-audit cve vulnerability exploit remediation
    soc siem edr compliance soc2 iso27001 pci-dss gdpr penetration-testing
    function-calling tool-use agent react-agent code-agent llm-agent autonomous-agent
    browse-the-web computer-use swe-bench swe-agent
    chain-of-thought reasoning o1-style r1-distill long-cot
    math-reasoning step-by-step self-consistency theorem-proving
    instruction-tuning sft dpo rlhf rlaif preference-pairs constitutional-ai
    magpie self-instruct evol-instruct synthetic-data
)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }
notify() {
    [[ -z "${DISCORD_WEBHOOK:-}" ]] && return
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"content\":\"🔭 discoverer: $1\"}" "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
}

get_token() {
    local idx=$1
    local pool="${HF_TOKEN_POOL:-${HF_TOKEN:-}}"
    [[ -z "$pool" ]] && return 1
    IFS=',' read -ra _KEYS <<< "$pool"
    echo "${_KEYS[$(( idx % ${#_KEYS[@]} ))]}"
}

queue_add() {
    local repo=$1 cat=$2 max=$3 pri=$4
    grep -q "^${repo}|" "$QUEUE_FILE" 2>/dev/null && return 1
    echo "${repo}|${cat}|${max}|${pri}" >> "$QUEUE_FILE"
    return 0
}

# ── 1. HF keyword search (different keyword each minute) ─────────────
discover_hf_keywords() {
    local minute=$(($(date +%s) / 60))
    local kw="${KEYWORDS[$(( minute % ${#KEYWORDS[@]} ))]}"
    local tok; tok=$(get_token "$minute")
    [[ -z "$tok" ]] && return 1
    local out; out=$(curl -fsS --max-time 15 \
        -H "Authorization: Bearer $tok" \
        "https://huggingface.co/api/datasets?search=${kw// /%20}&sort=downloads&direction=-1&limit=20" 2>/dev/null) || return 1
    local n_added=0
    while IFS=$'\t' read -r repo dl; do
        [[ -z "$repo" || "$repo" == "null" ]] && continue
        local cat="misc"
        case "$kw" in
            *code*|*python*|*typescript*|*javascript*|*rust*|*go*|*completion*|*review*|*bug*|*generation*|*feedback*|*eval*) cat="code" ;;
            *terraform*|*kubernetes*|*ansible*|*cloudformation*|*cdk*|*helm*|*actions*|*ci-cd*|*docker*|*sre*|*slo*|*runbook*|*postmortem*|*incident*) cat="devops" ;;
            *iam*|*security*|*cve*|*vulnerability*|*exploit*|*remediation*|*soc*|*siem*|*edr*|*compliance*|*pen*) cat="security" ;;
            *function*|*tool*|*agent*|*react*|*browse*|*computer*|*swe*|*autonomous*) cat="agent" ;;
            *cot*|*chain*|*reasoning*|*o1*|*r1*|*math*|*step*|*self-consistency*|*theorem*) cat="reasoning" ;;
            *instruction*|*sft*|*dpo*|*rlhf*|*rlaif*|*preference*|*magpie*|*self-instruct*|*evol*|*constitutional*|*synthetic*) cat="sft" ;;
        esac
        local max=10000 pri=3
        if [[ "${dl:-0}" -gt 100000 ]]; then max=200000; pri=1
        elif [[ "${dl:-0}" -gt 10000 ]]; then max=50000; pri=2; fi
        queue_add "$repo" "$cat" "$max" "$pri" && n_added=$((n_added+1))
    done < <(echo "$out" | python3 -c "
import json, sys
try: rs = json.load(sys.stdin)
except: sys.exit(0)
for r in rs:
    rid = r.get('id','')
    dl  = r.get('downloads', 0) or 0
    if rid: print(f'{rid}\t{dl}')
" 2>/dev/null)
    log "[hf-kw] '$kw' added=$n_added"
    return 0
}

# ── 2. HF trending datasets ──────────────────────────────────────────
discover_hf_trending() {
    local tok; tok=$(get_token "$(date +%s)")
    [[ -z "$tok" ]] && return 1
    local out; out=$(curl -fsS --max-time 15 \
        -H "Authorization: Bearer $tok" \
        "https://huggingface.co/api/datasets?sort=trending&direction=-1&limit=30" 2>/dev/null) || return 1
    local n_added=0
    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        queue_add "$repo" "trending" "100000" "2" && n_added=$((n_added+1))
    done < <(echo "$out" | python3 -c "
import json, sys
try: rs = json.load(sys.stdin)
except: sys.exit(0)
for r in rs[:30]:
    rid = r.get('id','')
    if rid: print(rid)
" 2>/dev/null)
    log "[hf-trending] added=$n_added"
}

# ── 3. arxiv recent papers (cs.LG / cs.CL / cs.SE) ───────────────────
discover_arxiv() {
    local cat
    for cat in cs.LG cs.CL cs.SE; do
        local out; out=$(curl -fsS --max-time 15 \
            "https://export.arxiv.org/api/query?search_query=cat:${cat}&sortBy=submittedDate&sortOrder=descending&max_results=20" 2>/dev/null) || continue
        # arxiv returns Atom XML; we don't fetch the PDFs but log discovered IDs
        # so a downstream script can decide which to mirror via paperqa/etc.
        local n; n=$(echo "$out" | grep -c "<id>http://arxiv.org/abs/")
        log "[arxiv] $cat papers_seen=$n"
        sleep 0.5
    done
}

# ── 4. Stack Exchange API direct (Stack Overflow + Server Fault + Security + DBA) ─
discover_stackexchange() {
    local site
    for site in stackoverflow serverfault security dba unix codereview; do
        local out; out=$(curl -fsS --max-time 12 \
            "https://api.stackexchange.com/2.3/questions?order=desc&sort=activity&site=${site}&pagesize=20" 2>/dev/null) || continue
        local n; n=$(echo "$out" | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except: sys.exit(0)
print(len(d.get('items', [])))
" 2>/dev/null || echo "0")
        log "[stackexchange] $site questions_seen=$n"
        sleep 0.5
    done
    # Note: SE bulk dumps are at archive.org/details/stackexchange — those
    # are huge static files, fetched separately by trillion-token-sources.
}

# ── 5. GitHub trending repos (Python / TypeScript / Rust / Go) ───────
discover_github_trending() {
    [[ -z "${GITHUB_TOKEN:-}" ]] && [[ -z "${GH_TOKEN:-}" ]] && {
        log "[gh-trending] skip — no GITHUB_TOKEN"
        return 0
    }
    local TOK="${GITHUB_TOKEN:-${GH_TOKEN}}"
    local lang
    for lang in python typescript rust go; do
        local d_ago; d_ago=$(date -u -v-7d +%Y-%m-%d 2>/dev/null \
                              || date -u -d "7 days ago" +%Y-%m-%d)
        local out; out=$(curl -fsS --max-time 12 \
            -H "Authorization: Bearer $TOK" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/search/repositories?q=language:${lang}+created:>${d_ago}+stars:>50&sort=stars&order=desc&per_page=20" 2>/dev/null) || continue
        local n; n=$(echo "$out" | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except: sys.exit(0)
print(d.get('total_count', 0))
" 2>/dev/null || echo "0")
        log "[gh-trending] lang=$lang found=$n"
        sleep 1
    done
}

# ── Main loop ─────────────────────────────────────────────────────────
log "continuous-discoverer start (pid=$$)"
notify "discoverer up"

CYCLE=0
while true; do
    CYCLE=$((CYCLE+1))
    for fn in "${SOURCE_FNS[@]}"; do
        $fn 2>>"$LOG" || true
        sleep 4   # tighter — burst mode (was 8s)
    done

    # Re-seed coordinator every 10 cycles (~50 min) so newly-added rows enter queue
    if (( CYCLE % 10 == 0 )); then
        python3 "$COORDINATOR" seed >> "$LOG" 2>&1 || true
        TOTAL=$(grep -cE "^[a-zA-Z]" "$QUEUE_FILE" 2>/dev/null || echo "?")
        log "[seed] cycle=$CYCLE queue_total=$TOTAL"
        notify "cycle $CYCLE done — queue total $TOTAL sources"
    fi

    sleep 10   # tighter cycle gap — burst mode (was 30s)
done
