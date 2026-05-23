#!/usr/bin/env bash
# Auto-scrape top GitHub repos across Ops/Engineering topics.
# Discover → shallow clone → extract knowledge → ingest → delete.
# Runs via cron. Dedups via seen-list. Disk-aware.
set -u

WORK="/tmp/github-ops-scrape"
SEEN="$HOME/.claude/state/github-ops-seen.txt"
LOG="$HOME/.claude/logs/scrape-github-ops.log"
MIN_FREE_GB=5
STARS_MIN=500         # only repos with ≥500 stars
TOP_PER_TOPIC=5       # top 5 fresh repos per topic per run

mkdir -p "$WORK" "$(dirname "$SEEN")" "$(dirname "$LOG")"
touch "$SEEN"

echo "[$(date '+%Y-%m-%d %H:%M')] === Starting GitHub ops scrape ===" | tee -a "$LOG"

# All Ops / Engineering topics to crawl
TOPICS=(
    # DevSecOps / SRE / Platform
    devsecops sre site-reliability-engineering platform-engineering internal-developer-platform
    # CI/CD / GitOps
    ci-cd gitops continuous-delivery deployment-automation release-engineering
    # Cloud / Infra
    cloud-native kubernetes terraform crossplane helm serverless infrastructure-as-code
    # Observability
    observability opentelemetry prometheus grafana monitoring distributed-tracing incident-response
    # AI / ML Ops
    mlops aiops llmops ai-infrastructure ai-platform ml-platform model-deployment
    # FinOps / DBOps
    finops cost-optimization dbops database-reliability chaos-engineering
    # AI Dev
    ai-agent llm-agent rag retrieval-augmented-generation agent-framework
    # Security
    devsec cloud-security supply-chain-security zero-trust
)

COUNT=0
for TOPIC in "${TOPICS[@]}"; do
    FREE_GB=$(df -g ~ | tail -1 | awk '{print $4}')
    if [[ "$FREE_GB" -lt "$MIN_FREE_GB" ]]; then
        echo "⚠ disk low ($FREE_GB GB) — stop" | tee -a "$LOG"
        break
    fi

    echo "  [topic] $TOPIC" | tee -a "$LOG"

    # Search top repos by stars
    RESULT=$(gh api -X GET "search/repositories" \
        -f q="topic:$TOPIC stars:>$STARS_MIN" \
        -f sort=stars -f order=desc -F per_page=10 2>/dev/null || echo '{"items":[]}')

    REPOS=$(echo "$RESULT" | ~/.claude/venv/bin/python -c "
import json, sys
try:
    d = json.load(sys.stdin)
except:
    print(''); sys.exit(0)
seen = set(open('$SEEN').read().splitlines())
n = 0
for r in d.get('items', [])[:30]:
    full = r.get('full_name','')
    if not full or full in seen: continue
    if r.get('fork') or r.get('archived'): continue
    if r.get('size', 0) > 500_000: continue  # skip >500MB
    default_br = r.get('default_branch','main')
    print(f\"{full}|{default_br}\")
    n += 1
    if n >= $TOP_PER_TOPIC: break
")

    [[ -z "$REPOS" ]] && continue

    while IFS='|' read -r FULL_NAME BRANCH; do
        [[ -z "$FULL_NAME" ]] && continue
        SAFE_NAME=$(echo "$FULL_NAME" | tr '/' '_')
        CLONE_DIR="$WORK/$SAFE_NAME"
        rm -rf "$CLONE_DIR"

        # Disk pre-check
        FREE_GB=$(df -g ~ | tail -1 | awk '{print $4}')
        [[ "$FREE_GB" -lt "$MIN_FREE_GB" ]] && { echo "  ⚠ disk low" | tee -a "$LOG"; break; }

        echo "  [$(date '+%H:%M:%S')] $FULL_NAME ($BRANCH)" | tee -a "$LOG"

        if git clone --depth=1 --single-branch --branch "$BRANCH" \
            --filter=blob:limit=1m \
            "https://github.com/$FULL_NAME.git" "$CLONE_DIR" >>"$LOG" 2>&1; then
            INDEX_ROOT="$CLONE_DIR" REPO_NAME="github:$FULL_NAME" TOPIC="$TOPIC" \
                /opt/surrogate-1-harvest/bin/with-chroma-lock.sh ~/.claude/venv/bin/python /opt/surrogate-1-harvest/bin/index-repo-public.py >>"$LOG" 2>&1
            echo "$FULL_NAME" >> "$SEEN"
            COUNT=$((COUNT+1))
        else
            echo "  clone failed" | tee -a "$LOG"
        fi

        rm -rf "$CLONE_DIR"
    done <<< "$REPOS"
done

rm -rf "$WORK"
echo "[$(date '+%Y-%m-%d %H:%M')] === Done: $COUNT new repos indexed ===" | tee -a "$LOG"
