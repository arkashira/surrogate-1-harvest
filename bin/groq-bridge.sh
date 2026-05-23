#!/usr/bin/env bash
# Groq bridge — fast Llama/Qwen inference via Groq API (OpenAI-compat)
# Usage: groq-bridge.sh [--model MODEL] "<prompt>"  |  echo "..." | groq-bridge.sh
set -u
# Default: Llama 3.3 70B — best quality on Groq free tier (still ultra-fast).
# 8B is available as --model fast when latency matters more than quality.
MODEL="llama-3.3-70b-versatile"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                fast|small|8b) MODEL="llama-3.1-8b-instant" ;;
                llama|llama70) MODEL="llama-3.3-70b-versatile" ;;
                qwen)          MODEL="qwen/qwen3-32b" ;;
                llama4|scout)  MODEL="meta-llama/llama-4-scout-17b-16e-instruct" ;;
                gpt-oss|oss)   MODEL="openai/gpt-oss-120b" ;;
                *)             MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "groq-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/groq-bridge.log"
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
}
try:
    d = request_with_retry(
        'https://api.groq.com/openai/v1/chat/completions',
        data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json', 'User-Agent':'hermes-agent/1.0', 'Authorization':'Bearer '+os.environ.get('GROQ_API_KEY','')},
        timeout=120, max_retries=4, base_delay=2.0,
    )
    print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
except Exception as e:
    print(f'groq-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
