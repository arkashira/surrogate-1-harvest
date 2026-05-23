#!/usr/bin/env bash
# OpenRouter bridge — meta-router across many providers.
# Free models: qwen/qwen-2.5-coder-32b-instruct:free, deepseek/deepseek-r1:free,
# meta-llama/llama-3.3-70b-instruct:free, google/gemini-2.0-flash-exp:free.
#
# Usage (matches cerebras/groq/chutes interface):
#   echo "<prompt>" | openrouter-bridge.sh [--model fast|big|free|<id>] [--max-tokens N]
#   openrouter-bridge.sh "prompt as arg"
set -u
MODEL="meta-llama/llama-3.3-70b-instruct:free"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                fast|small)  MODEL="meta-llama/llama-3.3-70b-instruct:free" ;;
                big)         MODEL="deepseek/deepseek-r1:free" ;;
                code|coder)  MODEL="meta-llama/llama-3.3-70b-instruct:free" ;;
                gemini)      MODEL="google/gemini-2.0-flash-exp:free" ;;
                free)        MODEL="meta-llama/llama-3.3-70b-instruct:free" ;;
                *)           MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "openrouter-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/openrouter-bridge.log"
mkdir -p "$(dirname "$LOG")"
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env"; set +a; }
echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

# Pool support: if OPENROUTER_POOL is set (csv of keys), pick one round-robin.
# Else try OPENROUTER_API_KEY → OPENROUTER_API_KEY_2 → OPENROUTER_API_KEY_3.
if [[ -n "${OPENROUTER_POOL:-}" ]]; then
    IFS=',' read -ra _KEYS <<< "$OPENROUTER_POOL"
    _N=${#_KEYS[@]}
    _IDX=$(( ($(date +%s) / 30) % _N ))
    OPENROUTER_API_KEY="${_KEYS[$_IDX]}"
fi
# Auto-fallback: if primary 401s, the python below retries with _2 then _3
OR_KEYS=""
for k in OPENROUTER_API_KEY OPENROUTER_API_KEY_2 OPENROUTER_API_KEY_3; do
    v="${!k:-}"
    [[ -n "$v" ]] && OR_KEYS="${OR_KEYS}${OR_KEYS:+,}${v}"
done

RESPONSE=$(MODEL="$MODEL" MAX_TOKENS="$MAX_TOKENS" TEMP="$TEMP" OR_KEYS="$OR_KEYS" \
python3 -c "
import json, os, sys, urllib.request, urllib.error
keys = [k for k in os.environ.get('OR_KEYS','').split(',') if k]
if not keys:
    print('openrouter-bridge: no OPENROUTER_API_KEY*', file=sys.stderr); sys.exit(2)
body = {
    'model': os.environ['MODEL'],
    'messages': [{'role':'user','content': sys.stdin.read()}],
    'max_tokens': int(os.environ['MAX_TOKENS']),
    'temperature': float(os.environ['TEMP']),
}
data = json.dumps(body).encode()
last_err = ''
for key in keys:
    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        data=data,
        headers={
            'Content-Type':'application/json',
            'Authorization':'Bearer '+key,
            'HTTP-Referer':'https://axentx.dev/surrogate-1',
            'X-Title':'Surrogate-1',
        })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
        sys.exit(0)
    except urllib.error.HTTPError as e:
        last_err = f'HTTP {e.code}: {e.read().decode(\"utf-8\",\"ignore\")[:300]}'
        if e.code in (401, 403, 429):
            continue   # try next key
        break
    except Exception as e:
        last_err = str(e); break
print(f'openrouter-bridge {last_err}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
