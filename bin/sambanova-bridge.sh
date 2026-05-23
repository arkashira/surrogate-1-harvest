#!/usr/bin/env bash
# SambaNova Cloud bridge — fast Llama 3.3 70B/405B + DeepSeek-V3 free tier
# Endpoint: https://api.sambanova.ai/v1 (OpenAI-compat, ~500 tok/s)
# Key env:  SAMBANOVA_API_KEY
# Usage:    sambanova-bridge.sh [--model MODEL] "<prompt>"
set -u
# Default: Llama 3.3 70B — best speed (500 tok/s) × quality tradeoff on SambaNova.
# Full catalog verified 2026-04: DeepSeek-V3.1/V3.1-cb/V3.2, Llama-4-Maverick,
# gpt-oss-120b, gemma-3-12b-it, MiniMax-M2.5 (service-tier-locked).
MODEL="Meta-Llama-3.3-70B-Instruct"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                fast|small|gemma|gemma3)  MODEL="gemma-3-12b-it" ;;
                llama|llama70|70b)        MODEL="Meta-Llama-3.3-70B-Instruct" ;;
                llama4|maverick)          MODEL="Llama-4-Maverick-17B-128E-Instruct" ;;
                deepseek|deepseek-v3)     MODEL="DeepSeek-V3.1" ;;
                deepseek-latest|v32)      MODEL="DeepSeek-V3.2" ;;
                deepseek-cb|cb)           MODEL="DeepSeek-V3.1-cb" ;;
                gpt-oss|oss|120b)         MODEL="gpt-oss-120b" ;;
                *)                        MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "sambanova-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/sambanova-bridge.log"
mkdir -p "$(dirname "$LOG")"
set -a; source "$HOME/.hermes/.env" 2>/dev/null || true; set +a

if [[ -z "${SAMBANOVA_API_KEY:-}" ]]; then
    echo "sambanova-bridge: missing SAMBANOVA_API_KEY in ~/.hermes/.env" >&2
    exit 3
fi

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
}
try:
    d = request_with_retry(
        'https://api.sambanova.ai/v1/chat/completions',
        data=json.dumps(body).encode(),
        headers={
            'Content-Type':'application/json',
            'User-Agent':'hermes-agent/1.0',
            'Authorization':'Bearer '+os.environ.get('SAMBANOVA_API_KEY',''),
        },
        timeout=120, max_retries=4, base_delay=2.0,
    )
    print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
except Exception as e:
    print(f'sambanova-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
