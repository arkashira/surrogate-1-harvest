#!/usr/bin/env bash
# Surrogate-1 bridge — local Ollama endpoint for the Ashira-personalized model.
# Currently uses base Qwen2.5-Coder-7B + Thai/DevSecOps SYSTEM prompt as placeholder.
# After LoRA training on RunPod, rebuild Ollama model with merged adapter.
# Model URL: http://localhost:11434 (Ollama)
set -u
MODEL="surrogate-1"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "surrogate-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/surrogate-bridge.log"
mkdir -p "$(dirname "$LOG")"
echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

# Ollama OpenAI-compat endpoint
RESPONSE=$(python3 -c "
import json, sys, urllib.request, urllib.error

body = {
    'model': '$MODEL',
    'messages': [{'role':'user','content': sys.stdin.read()}],
    'max_tokens': $MAX_TOKENS,
    'temperature': $TEMP,
    'stream': False,
}
req = urllib.request.Request(
    'http://localhost:11434/v1/chat/completions',
    data=json.dumps(body).encode(),
    headers={'Content-Type':'application/json','Authorization':'Bearer ollama'}
)
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
except Exception as e:
    print(f'surrogate-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
