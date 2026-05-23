#!/usr/bin/env bash
# NVIDIA NIM bridge — OpenAI-compat via integrate.api.nvidia.com
# Free tier: ~1000 req/day, 50+ models (Llama, DeepSeek, Nemotron, Qwen, etc.)
set -u
MODEL="meta/llama-3.3-70b-instruct"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                llama|l70)     MODEL="meta/llama-3.3-70b-instruct" ;;
                nemotron)      MODEL="nvidia/nemotron-4-340b-instruct" ;;
                nemotron-nano) MODEL="nvidia/nemotron-3-nano-9b-v1" ;;
                deepseek|r1)   MODEL="deepseek-ai/deepseek-r1" ;;
                qwen|coder)    MODEL="qwen/qwen2.5-coder-32b-instruct" ;;
                mistral)       MODEL="mistralai/mistral-large-2-instruct" ;;
                *)             MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "nvidia-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/nvidia-bridge.log"
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
        'https://integrate.api.nvidia.com/v1/chat/completions',
        data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json', 'User-Agent':'hermes-agent/1.0', 'Authorization':'Bearer '+os.environ.get('NVIDIA_API_KEY','')},
        timeout=120, max_retries=4, base_delay=3.0, open_seconds=120,
    )
    print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
except Exception as e:
    print(f'nvidia-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
