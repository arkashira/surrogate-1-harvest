#!/usr/bin/env bash
# ZeroGPU bridge — calls our own Surrogate-1 Space (ashirato/surrogate-1-zero-gpu)
# which serves Qwen2.5-Coder-7B + Surrogate-1 v1 LoRA on free PRO ZeroGPU A10G.
#
# Cold-start ~5-10s, then 60-120s of GPU per request. Up to 25K GPU-min/mo
# total under PRO subscription.
#
# Usage:
#   echo "<prompt>" | zero-gpu-bridge.sh [--max-tokens N] [--temperature T]
set -u
SPACE_URL="${ZERO_GPU_SPACE_URL:-https://ashirato-surrogate-1-zero-gpu.hf.space}"
MAX_TOKENS=512
TEMP=0.4
TOP_P=0.9
PROMPT=""
HISTORY="[]"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        --top-p) TOP_P="$2"; shift 2 ;;
        --space-url) SPACE_URL="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "zero-gpu-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/zero-gpu-bridge.log"
mkdir -p "$(dirname "$LOG")"
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env"; set +a; }

HF_TOKEN_USE="${HF_TOKEN_PRO:-${HF_TOKEN:-}}"
echo "[$(date '+%H:%M:%S')] space=$SPACE_URL len=${#PROMPT}" >> "$LOG"

RESPONSE=$(SPACE="$SPACE_URL" MAX_TOKENS="$MAX_TOKENS" TEMP="$TEMP" TOP_P="$TOP_P" \
    HF_TOKEN_USE="$HF_TOKEN_USE" \
python3 -c "
import json, os, re, sys, time, urllib.request, urllib.error
prompt = sys.stdin.read()
space = os.environ['SPACE']
# Gradio 4.44 with .queue() rejects POST /api/predict and POST
# /run/<api_name> ('This API endpoint does not accept direct HTTP POST
# requests. Please join the queue.') — must use /call/<api_name> with
# event_id polling. The Space app.py exposes api_name='respond' for
# the chat function (signature: respond(message) -> str).
hdr = {'Content-Type':'application/json'}
tok = os.environ.get('HF_TOKEN_USE','')
if tok: hdr['Authorization'] = 'Bearer ' + tok


def call_once(attempt: int) -> tuple[str, str]:
    '''Returns (output, err). On success: (text, ''); on fail: ('', reason).'''
    # Step 1: enqueue
    try:
        req = urllib.request.Request(
            f'{space}/call/respond',
            data=json.dumps({'data':[prompt]}).encode(), headers=hdr)
        with urllib.request.urlopen(req, timeout=30) as r:
            eid = json.load(r).get('event_id','')
    except urllib.error.HTTPError as e:
        return '', f'enqueue HTTP {e.code}: {e.read().decode(\"utf-8\",\"ignore\")[:200]}'
    except Exception as e:
        return '', f'enqueue {type(e).__name__}: {e}'
    if not eid:
        return '', 'no event_id'

    # Step 2: poll SSE stream. Cold-start ~30-90s on first call after gc;
    # warm 5-15s. Allow up to 300s total to absorb worst-case load+LoRA.
    try:
        req = urllib.request.Request(f'{space}/call/respond/{eid}', headers=hdr)
        with urllib.request.urlopen(req, timeout=300) as r:
            body = r.read().decode('utf-8','ignore')
    except urllib.error.HTTPError as e:
        return '', f'poll HTTP {e.code}: {e.read().decode(\"utf-8\",\"ignore\")[:200]}'
    except Exception as e:
        return '', f'poll {type(e).__name__}: {e}'

    blocks = re.findall(r'event:\s*complete\s*\ndata:\s*(.*)', body)
    if not blocks:
        errs = re.findall(r'event:\s*error\s*\ndata:\s*(.*)', body)
        if errs:
            return '', f'SSE error: {errs[-1][:200]}'
        return '', f'no complete event in {len(body)}b body'

    try:
        payload = json.loads(blocks[-1])
    except Exception as e:
        return '', f'json parse: {e}'
    out = payload[0] if isinstance(payload, list) and payload else ''
    if isinstance(out, str) and out.strip():
        return out, ''
    return '', f'empty/unexpected payload {str(payload)[:120]}'


# Two-attempt strategy: cold-start often returns empty/error on first hit
# while ZeroGPU allocates the A10G. Wait 4s and retry once.
last_err = ''
for attempt in range(2):
    out, err = call_once(attempt)
    if out:
        print(out)
        sys.exit(0)
    last_err = err
    if attempt == 0:
        time.sleep(4)  # ZeroGPU allocator warm-up window
print(f'zero-gpu-bridge: {last_err}', file=sys.stderr)
sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
