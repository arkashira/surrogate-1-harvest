#!/usr/bin/env bash
# Cerebras bridge — fastest inference (wafer-scale), llama/qwen/gpt-oss available
set -u
MODEL="llama3.1-8b"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                fast|small) MODEL="llama3.1-8b" ;;
                big)        MODEL="qwen-3-235b-a22b-instruct-2507" ;;
                gpt-oss)    MODEL="gpt-oss-120b" ;;
                glm)        MODEL="zai-glm-4.7" ;;
                *)          MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "cerebras-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/cerebras-bridge.log"
mkdir -p "$(dirname "$LOG")"
set -a; source "$HOME/.hermes/.env"; set +a
echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

RESPONSE=$(python3 -c "
import os
exec(open(os.path.expanduser('~/.surrogate/bin/lib/dns_fallback.py')).read())
import json, sys, os, urllib.request, urllib.error
body = {
    'model': '$MODEL',
    'messages': [{'role':'user','content': sys.stdin.read()}],
    'max_tokens': $MAX_TOKENS, 'temperature': $TEMP,
}
req = urllib.request.Request(
    'https://api.cerebras.ai/v1/chat/completions',
    data=json.dumps(body).encode(),
    headers={'Content-Type':'application/json', 'User-Agent':'hermes-agent/1.0', 'Authorization':'Bearer '+os.environ.get('CEREBRAS_API_KEY','')}
)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
except urllib.error.HTTPError as e:
    print(f'cerebras-bridge HTTP {e.code}: {e.read()[:200]}', file=sys.stderr)
    sys.exit(e.code // 100)
except Exception as e:
    print(f'cerebras-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
