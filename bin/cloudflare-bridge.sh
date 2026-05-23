#!/usr/bin/env bash
# Cloudflare Workers AI bridge — 10k neurons/day free tier
# Endpoint: https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/ai/v1 (OpenAI-compat)
# Key env:  CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID
# Usage:    cloudflare-bridge.sh [--model MODEL] "<prompt>"
set -u
# Default: gpt-oss-120b — 120B params, highest capability on CF Workers AI free tier.
# Catalog verified 2026-04 — aliases point to models that ACTUALLY respond.
MODEL="@cf/openai/gpt-oss-120b"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                fast|small|8b)        MODEL="@cf/meta/llama-3.1-8b-instruct-fp8" ;;
                llama|llama70|70b)    MODEL="@cf/meta/llama-3.3-70b-instruct-fp8-fast" ;;
                gpt-oss|oss|120b)     MODEL="@cf/openai/gpt-oss-120b" ;;
                deepseek|r1|reasoning) MODEL="@cf/deepseek-ai/deepseek-r1-distill-qwen-32b" ;;
                kimi|long-ctx)        MODEL="@cf/moonshotai/kimi-k2.6" ;;
                glm|glm4)             MODEL="@cf/zai-org/glm-4.7-flash" ;;
                *)                    MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "cloudflare-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/cloudflare-bridge.log"
mkdir -p "$(dirname "$LOG")"
set -a; source "$HOME/.hermes/.env" 2>/dev/null || true; set +a

TOKEN="${CLOUDFLARE_API_TOKEN:-${CF_API_TOKEN:-}}"
ACCOUNT="${CLOUDFLARE_ACCOUNT_ID:-${CF_ACCOUNT_ID:-}}"
if [[ -z "$TOKEN" ]] || [[ -z "$ACCOUNT" ]]; then
    echo "cloudflare-bridge: missing CLOUDFLARE_API_TOKEN or CLOUDFLARE_ACCOUNT_ID in ~/.hermes/.env" >&2
    exit 3
fi

echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

RESPONSE=$(CF_TOKEN="$TOKEN" CF_ACCOUNT="$ACCOUNT" python3 -c "
import os
exec(open(os.path.expanduser('~/.surrogate/bin/lib/dns_fallback.py')).read())
exec(open(os.path.expanduser('~/.surrogate/bin/lib/bridge_retry.py')).read())
import json, sys
body = {
    'model': '$MODEL',
    'messages': [{'role':'user','content': sys.stdin.read()}],
    'max_tokens': $MAX_TOKENS, 'temperature': $TEMP,
}
url = f\"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT']}/ai/v1/chat/completions\"
try:
    d = request_with_retry(
        url,
        data=json.dumps(body).encode(),
        headers={
            'Content-Type':'application/json',
            'User-Agent':'hermes-agent/1.0',
            'Authorization':'Bearer '+os.environ['CF_TOKEN'],
        },
        timeout=120, max_retries=6, base_delay=5.0, open_seconds=180,
    )
    print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
except Exception as e:
    print(f'cloudflare-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
