#!/usr/bin/env bash
# Surrogate-1 v2 — synth-puller.
#
# Hits surrogate1/surrogate-1-zero-gpu Space's /api/predict on synth_batch
# endpoint to drain free PRO ZeroGPU budget into training data. Each call
# returns 10-20 Magpie-style training pairs.
#
# Cron: every 5 min, rotates through 16 domains. ~200-400 pairs/hr/Space.
# Combined with judge calls + best-of-n, drains ~25-30K min/mo per Space
# = both PRO accounts contributing to data factory.
#
# Output: /data/v2/synth/{domain}-{date}.jsonl (HF Space) — feeds into
# enrich-pipeline + push-training-to-hf next cycles.
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

# Round-robin between 2 PRO ZeroGPU endpoints (combined 50K min/mo).
# Each minute picks one — over an hour each gets ~30 calls so the per-account
# quota burn is balanced. Override with SYNTH_SPACE_URL env to force one.
SPACE_POOL=(
    "https://ashirato-surrogate-1-zero-gpu.hf.space"
    "https://surrogate1-surrogate-1-zero-gpu.hf.space"
)
if [[ -n "${SYNTH_SPACE_URL:-}" ]]; then
    SPACE_URL="$SYNTH_SPACE_URL"
else
    M=$(($(date +%s) / 60))
    SPACE_URL="${SPACE_POOL[$(( M % ${#SPACE_POOL[@]} ))]}"
fi
OUT_DIR="${HOME}/.surrogate/data/v2/synth"
LOG="${HOME}/.surrogate/logs/synth-puller.log"
mkdir -p "$OUT_DIR" "$(dirname "$LOG")"

DOMAINS=(
    code-python code-typescript code-rust code-go
    devops-tf devops-k8s devops-cdk ci-github
    sec-iam sec-cve sre-runbook sre-slo
    data-sql ai-eng api-rest test-pytest
)

# Rotate by minute so each tick hits a different domain
M=$(($(date +%s) / 60))
DOMAIN="${DOMAINS[$(( M % ${#DOMAINS[@]} ))]}"
COUNT="${SYNTH_COUNT:-12}"
DATE=$(date +%Y%m%d)
OUT="${OUT_DIR}/${DOMAIN}-${DATE}.jsonl"

echo "[$(date '+%H:%M:%S')] synth-puller domain=$DOMAIN count=$COUNT" >> "$LOG"

# Gradio 4.44 API: POST /call/<api_name> → returns {event_id} → GET
# /call/<api_name>/<event_id> streams server-sent events (SSE) until
# 'complete' with the result payload. The legacy /run/<api_name> path
# returns 'API endpoint does not accept direct HTTP POST' on queued
# Spaces (gradio 4.x with .queue() enabled), so this route is required.
TOK="${HF_TOKEN_PRO:-${HF_TOKEN:-}}"
EVT=$(curl -fsS --max-time 30 -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TOK}" \
    -d "{\"data\":[\"${DOMAIN}\", ${COUNT}]}" \
    "${SPACE_URL}/call/synth_batch" 2>&1 \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('event_id',''))" 2>/dev/null)

if [[ -z "$EVT" ]]; then
    echo "[$(date '+%H:%M:%S')] FAIL — no event_id (Space cold/down)" >> "$LOG"
    exit 1
fi

# Poll the SSE stream. Cold-start can take 60-120s for model load on
# first hit; warm calls return in 30-60s for count=12 pairs.
RESP=$(curl -fsS --max-time 320 \
    -H "Authorization: Bearer ${TOK}" \
    "${SPACE_URL}/call/synth_batch/${EVT}" 2>&1)

# SSE format: 'event: complete\ndata: ["<jsonl-string>"]\n\n' (last block).
JSONL=$(echo "$RESP" | python3 -c "
import json, re, sys
text = sys.stdin.read()
# Find 'event: complete' block; data line is the JSON list
blocks = re.findall(r'event:\s*complete\s*\ndata:\s*(.*)', text)
if not blocks:
    print('', file=sys.stderr); sys.exit(1)
try:
    d = json.loads(blocks[-1])
    out = (d or [''])[0]
    print(out if isinstance(out, str) else '')
except Exception:
    print('', file=sys.stderr); sys.exit(1)
")

if [[ -z "$JSONL" ]]; then
    echo "[$(date '+%H:%M:%S')] FAIL — no JSONL in response: $(echo $RESP | head -c 200)" >> "$LOG"
    exit 1
fi

# Append to today's domain file
N=$(echo "$JSONL" | wc -l | tr -d ' ')
echo "$JSONL" >> "$OUT"
echo "[$(date '+%H:%M:%S')] +$N pairs domain=$DOMAIN → $OUT" >> "$LOG"

# Discord notify on big runs
if [[ -n "${DISCORD_WEBHOOK:-}" ]] && (( N >= 10 )); then
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"content\":\"🧪 synth-puller: +$N pairs ($DOMAIN) from ZeroGPU\"}" \
        "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
fi
