#!/usr/bin/env bash
# Gemini bridge — Google AI Studio free tier (15 RPM, 1M tokens/day on flash).
# Models: gemini-2.5-pro (paid), gemini-2.5-flash (free), gemini-2.0-flash-exp (free).
#
# Usage:
#   echo "<prompt>" | gemini-bridge.sh [--model fast|pro] [--max-tokens N]
#   gemini-bridge.sh "prompt as arg"
set -u
MODEL="gemini-2.5-flash"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                fast|small)  MODEL="gemini-2.5-flash" ;;
                pro|big)     MODEL="gemini-2.5-pro" ;;
                exp)         MODEL="gemini-2.0-flash-exp" ;;
                lite)        MODEL="gemini-2.5-flash-lite" ;;
                *)           MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "gemini-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/gemini-bridge.log"
mkdir -p "$(dirname "$LOG")"
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env"; set +a; }
echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

KEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}"
RESPONSE=$(MODEL="$MODEL" MAX_TOKENS="$MAX_TOKENS" TEMP="$TEMP" KEY="$KEY" \
python3 -c "
import json, os, sys, urllib.request, urllib.error
key = os.environ.get('KEY','')
if not key:
    print('gemini-bridge: no GEMINI_API_KEY/GOOGLE_API_KEY', file=sys.stderr); sys.exit(2)
model = os.environ['MODEL']
body = {
    'contents': [{'parts':[{'text': sys.stdin.read()}]}],
    'generationConfig': {
        'maxOutputTokens': int(os.environ['MAX_TOKENS']),
        'temperature': float(os.environ['TEMP']),
    },
}
url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
req = urllib.request.Request(url,
    data=json.dumps(body).encode(),
    headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    cand = d.get('candidates',[{}])[0]
    parts = cand.get('content',{}).get('parts',[])
    text = ''.join(p.get('text','') for p in parts)
    if not text and cand.get('finishReason'):
        print(f'gemini-bridge: finish_reason={cand[\"finishReason\"]}', file=sys.stderr); sys.exit(1)
    print(text)
except urllib.error.HTTPError as e:
    msg = e.read().decode('utf-8','ignore')[:400]
    print(f'gemini-bridge HTTP {e.code}: {msg}', file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f'gemini-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
