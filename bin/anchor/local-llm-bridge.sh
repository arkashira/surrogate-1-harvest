#!/usr/bin/env bash
# Anchor-local LLM bridge — talks to local Ollama on the A1.Flex itself.
# Cheaper than free-tier cloud bridges + no network round-trip.
#
# Anchor pre-loaded models (per cloud-init):
#   qwen2.5-coder:7b  (~4.5 GB, primary)
#   qwen2.5-coder:3b  (~2 GB, faster fallback)
#
# Usage (matches the other bridges in surrogate-1/bin/):
#   echo "<prompt>" | local-llm-bridge.sh [--model 3b|7b] [--max-tokens N]
set -u
MODEL="qwen2.5-coder:7b"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""
HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                3b|small|fast) MODEL="qwen2.5-coder:3b" ;;
                7b|big)        MODEL="qwen2.5-coder:7b" ;;
                *)             MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens)  MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "local-llm-bridge: no prompt" >&2; exit 2; }

# Path-adaptive log: prefer Space-native /data/logs (anchor + bulk Space),
# fall back to $HOME/.surrogate/logs (Mac dev / non-Space hosts).
if [[ -d /data ]] && [[ -w /data ]] || mkdir -p /data/logs 2>/dev/null; then
    LOG="/data/logs/local-llm-bridge.log"
else
    LOG="${HOME}/.surrogate/logs/local-llm-bridge.log"
fi
mkdir -p "$(dirname "$LOG")" 2>/dev/null || LOG="/dev/null"
echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

RESPONSE=$(MODEL="$MODEL" MAX_TOKENS="$MAX_TOKENS" TEMP="$TEMP" HOST="$HOST" \
python3 -c "
import json, os, sys, urllib.request, urllib.error
body = {
    'model': os.environ['MODEL'],
    'messages': [{'role':'user','content': sys.stdin.read()}],
    'stream': False,
    'options': {
        'num_predict': int(os.environ['MAX_TOKENS']),
        'temperature': float(os.environ['TEMP']),
    },
}
req = urllib.request.Request(
    f\"http://{os.environ['HOST']}/api/chat\",
    data=json.dumps(body).encode(),
    headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    print((d.get('message') or {}).get('content',''))
except urllib.error.HTTPError as e:
    print(f'local-llm-bridge HTTP {e.code}: {e.read().decode(\"utf-8\",\"ignore\")[:300]}', file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f'local-llm-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
