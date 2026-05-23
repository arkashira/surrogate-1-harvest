#!/usr/bin/env bash
# GitHub Models bridge — free-tier GPT-4o / Llama 3.3 / Mistral via GitHub PAT
# Endpoint: https://models.github.ai/inference (OpenAI-compat)
# Key env:  GITHUB_MODELS_TOKEN (preferred) or GITHUB_TOKEN
# Usage:    github-bridge.sh [--model MODEL] "<prompt>" | echo "..." | github-bridge.sh
set -u
# Default: full GPT-4o (free via PAT, far smarter than mini, same daily quota)
MODEL="openai/gpt-4o"
MAX_TOKENS=2000
TEMP=0.3
PROMPT=""

# Aliases reflect ONLY models verified working with free PAT (2026-04).
# GPT-5/o3/o1-mini etc. appear in /catalog but API returns 403/unavailable — not usable.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            case "$2" in
                # OpenAI
                gpt4o|gpt-4o)               MODEL="openai/gpt-4o" ;;
                mini|gpt-4o-mini)           MODEL="openai/gpt-4o-mini" ;;
                gpt41|gpt-4.1)              MODEL="openai/gpt-4.1" ;;
                gpt41-mini|gpt-4.1-mini)    MODEL="openai/gpt-4.1-mini" ;;
                # Meta Llama
                llama|llama70)              MODEL="meta/Llama-3.3-70B-Instruct" ;;
                llama4|maverick)            MODEL="meta/llama-4-maverick-17b-128e-instruct-fp8" ;;
                llama405)                   MODEL="meta/meta-llama-3.1-405b-instruct" ;;
                # DeepSeek
                deepseek|deepseek-v3)       MODEL="deepseek/deepseek-v3-0324" ;;
                deepseek-r1|r1|reasoning)   MODEL="deepseek/DeepSeek-R1" ;;
                deepseek-r1-latest)         MODEL="deepseek/deepseek-r1-0528" ;;
                # xAI
                grok|grok3)                 MODEL="xai/grok-3" ;;
                grok-mini)                  MODEL="xai/grok-3-mini" ;;
                # Mistral
                mistral|mistral-medium)     MODEL="mistral-ai/mistral-medium-2505" ;;
                codestral|code)             MODEL="mistral-ai/codestral-2501" ;;
                # Microsoft Phi
                phi|phi4)                   MODEL="microsoft/phi-4" ;;
                # Cohere
                cohere|command-a)           MODEL="cohere/cohere-command-a" ;;
                command-r)                  MODEL="cohere/cohere-command-r-plus-08-2024" ;;
                *)                          MODEL="$2" ;;
            esac; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --temperature) TEMP="$2"; shift 2 ;;
        *) PROMPT="$*"; break ;;
    esac
done
[[ -z "$PROMPT" ]] && [[ ! -t 0 ]] && PROMPT=$(cat)
[[ -z "$PROMPT" ]] && { echo "github-bridge: no prompt" >&2; exit 2; }

LOG="$HOME/.surrogate/logs/github-bridge.log"
mkdir -p "$(dirname "$LOG")"
set -a; source "$HOME/.hermes/.env" 2>/dev/null || true; set +a

# Prefer dedicated models token, fall back to general PAT
TOKEN="${GITHUB_MODELS_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -z "$TOKEN" ]]; then
    echo "github-bridge: missing GITHUB_MODELS_TOKEN or GITHUB_TOKEN in ~/.hermes/.env" >&2
    exit 3
fi

echo "[$(date '+%H:%M:%S')] model=$MODEL len=${#PROMPT}" >> "$LOG"

RESPONSE=$(GH_TOKEN="$TOKEN" python3 -c "
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
        'https://models.github.ai/inference/chat/completions',
        data=json.dumps(body).encode(),
        headers={
            'Content-Type':'application/json',
            'User-Agent':'hermes-agent/1.0',
            'Authorization':'Bearer '+os.environ['GH_TOKEN'],
        },
        timeout=120, max_retries=4, base_delay=2.0,
    )
    print(d.get('choices',[{}])[0].get('message',{}).get('content',''))
except Exception as e:
    print(f'github-bridge error: {e}', file=sys.stderr); sys.exit(1)
" <<< "$PROMPT")
RC=$?
echo "[$(date '+%H:%M:%S')] rc=$RC bytes=${#RESPONSE}" >> "$LOG"
[[ $RC -ne 0 ]] && exit $RC
echo "$RESPONSE"
