#!/usr/bin/env bash
# Chutes.ai bridge — OpenAI-compat; free-tier, multi-model aggregator.
# Endpoint: https://llm.chutes.ai/v1/chat/completions
# Free tier: ~500 req/day, no CC, solid for Qwen/DeepSeek/Llama models.
set -u
MODEL="deepseek-ai/DeepSeek-V3.1"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                deepseek|v3)   MODEL="deepseek-ai/DeepSeek-V3.1" ;;
                qwen|coder)    MODEL="Qwen/Qwen3-Coder-480B-A35B-Instruct" ;;
                llama|l70)     MODEL="meta-llama/Llama-3.3-70B-Instruct" ;;
                r1)            MODEL="deepseek-ai/DeepSeek-R1" ;;
                glm)           MODEL="zai-org/GLM-4.6" ;;
                *)             MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "chutes-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/chutes-bridge.log"
mkdir -p "$(dirname "$LOG")"
set -a; source "$HOME/.hermes/.env"; set +a
echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

RESPONSE=$(python3 -c "
import os
exec(open(os.path.expanduser('~/.surrogate/bin/lib/dns_fallback.py')).read())
exec(open(os.path.expanduser('~/.surrogate/bin/lib/bridge_retry.py')).read())
import json, sys
body = {
    'model': '$MODEL',
    'messages': [{'role':'user','content': sys.stdin.read()}],
    'max_tokens': $MAX_TOKENS, 'temperature': $TEMP,
    'stream': False,
}
try:
    d = request_with_retry(
        'https://llm.chutes.ai/v1/chat/completions',
        data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json', 'User-Agent':'hermes-agent/1.0', 'Authorization':'Bearer '+os.environ.get('CHUTES_API_KEY','')},
        timeout=120, max_retries=4, base_delay=3.0, open_seconds=120,
    )
    print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
except Exception as e:
    print(f'chutes-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
