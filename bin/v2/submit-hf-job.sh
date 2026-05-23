#!/usr/bin/env bash
# Submit an HF Job — runs arbitrary container/script on HF GPU infra.
# Requires admin role on org (we have it via HF_TOKEN = surrogate1 admin).
#
# Use cases:
#   • Stage1-SDFT training run (axolotl on H100)
#   • mergekit DARE-TIES merge of 9 cluster LoRAs
#   • Eval batch: EvalPlus + LiveCodeBench + BFCL on a fresh checkpoint
#
# Free PRO quota for HF Jobs is small but exists; check at hf.co/settings/billing.
# Beyond quota = pay-per-second on the chosen flavor.
#
# Usage:
#   submit-hf-job.sh --image <docker-img> --script <path> --flavor a10g-large
#   submit-hf-job.sh --train-stage1-sdft        # shortcut
#   submit-hf-job.sh --merge-9-loras            # shortcut
set -uo pipefail

[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env"; set +a; }

# Use HF_TOKEN (surrogate1 admin role — required for job.write scope)
TOKEN="${HF_TOKEN:-}"
[[ -z "$TOKEN" ]] && { echo "no HF_TOKEN" >&2; exit 2; }

IMAGE="huggingface/transformers-pytorch-gpu:latest"
SCRIPT=""
FLAVOR="a10g-large"
SECRETS_JSON='{"HF_TOKEN":"'"$TOKEN"'"}'
ENV_JSON='{}'
TIMEOUT_SEC=21600  # 6 hr default

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)   IMAGE="$2"; shift 2 ;;
        --script)  SCRIPT="$2"; shift 2 ;;
        --flavor)  FLAVOR="$2"; shift 2 ;;
        --timeout) TIMEOUT_SEC="$2"; shift 2 ;;
        --train-stage1-sdft)
            SCRIPT="cd /tmp && git clone --depth 1 https://huggingface.co/spaces/axentx/surrogate-1 src && cd src && bash bin/v2/run-phase-a.sh"
            FLAVOR="h200"
            TIMEOUT_SEC=43200  # 12 hr
            shift ;;
        --merge-9-loras)
            SCRIPT="cd /tmp && git clone --depth 1 https://huggingface.co/spaces/axentx/surrogate-1 src && cd src && MERGE_METHOD=dare_ties bash bin/v2/merge-9-loras.sh"
            FLAVOR="a10g-large"
            TIMEOUT_SEC=10800  # 3 hr
            shift ;;
        --eval-tier1)
            SCRIPT="cd /tmp && git clone --depth 1 https://huggingface.co/spaces/axentx/surrogate-1 src && cd src && bash bin/v2/eval-tier1.sh axentx/surrogate-1-coder-7b-v2-merged"
            FLAVOR="a10g-large"
            TIMEOUT_SEC=14400
            shift ;;
        *) echo "unknown: $1" >&2; exit 2 ;;
    esac
done

[[ -z "$SCRIPT" ]] && { echo "must pass --script or shortcut flag" >&2; exit 2; }

# Build job spec
SPEC=$(python3 -c "
import json
print(json.dumps({
    'arguments': ['/bin/bash', '-c', '$SCRIPT'],
    'environment': $ENV_JSON,
    'flavor': '$FLAVOR',
    'image': '$IMAGE',
    'secrets': $SECRETS_JSON,
    'timeoutSeconds': $TIMEOUT_SEC,
}))
")

echo "▶ submitting HF Job: flavor=$FLAVOR timeout=${TIMEOUT_SEC}s"
echo "  script: $(echo "$SCRIPT" | head -c 120)..."

# Submit via HF API (requires admin role + job.write scope on org — we have it)
RESP=$(curl -fsS -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$SPEC" \
    "https://huggingface.co/api/jobs/axentx" 2>&1)

if echo "$RESP" | grep -q '"id"'; then
    JOB_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))")
    echo "  ✓ job submitted: $JOB_ID"
    echo "  URL: https://huggingface.co/axentx/jobs/$JOB_ID"
    echo "$RESP" > "$HOME/.surrogate/logs/hf-job-${JOB_ID}.json"
else
    echo "  ❌ submission failed:"
    echo "$RESP" | head -c 600
    exit 1
fi
