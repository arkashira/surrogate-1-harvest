#!/usr/bin/env bash
# Ollama local-model bridge — drop-in fallback when paid APIs run out of credit.
# Reads JSON from stdin: {"messages":[...], "model":"..." (optional), "max_tokens":N}
# Writes plain text response to stdout.
#
# Falls through model preference:
#   1. qwen3-coder:30b-a3b   (primary brain, MoE 3B active, fast)
#   2. qwen2.5-coder:14b     (fallback, proven)
#   3. granite-code:8b       (light, 128K context)
#   4. gemma4:e4b            (very light triage)
#
# Picks first available model that's installed locally on this Space.

set -uo pipefail
PAYLOAD=$(cat)
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

# Wait if Ollama not listening yet (e.g., booting)
for i in 1 2 3 4 5; do
    if curl -fsSm 3 "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then break; fi
    sleep 2
done

# Discover available models, pick first that exists in priority order
AVAIL=$(curl -fsSm 5 "http://${OLLAMA_HOST}/api/tags" 2>/dev/null \
        | python3 -c "import sys, json; print('\n'.join(m['name'] for m in json.load(sys.stdin).get('models', [])))" 2>/dev/null)
if [[ -z "$AVAIL" ]]; then
    echo "{\"error\":\"ollama not reachable at $OLLAMA_HOST\"}" >&2
    exit 1
fi

CHOSEN=""
for pref in "qwen3-coder:30b-a3b-instruct-q4_K_M" "qwen3-coder" \
            "qwen2.5-coder:14b-instruct-q4_K_M" "qwen2.5-coder:14b" "qwen2.5-coder" \
            "granite-code:8b-instruct" "granite-code:8b" "granite-code" \
            "gemma4:e4b" "gemma4" "gemma3:1b" "gemma3" "llama3.2:3b" "llama3.2"; do
    if echo "$AVAIL" | grep -qE "^${pref}(:|$)"; then
        CHOSEN=$(echo "$AVAIL" | grep -E "^${pref}(:|$)" | head -1)
        break
    fi
done

if [[ -z "$CHOSEN" ]]; then
    # Use whatever is first
    CHOSEN=$(echo "$AVAIL" | head -1)
fi

# User can override by setting model in payload
USER_MODEL=$(echo "$PAYLOAD" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('model','') or '')" 2>/dev/null)
if [[ -n "$USER_MODEL" ]] && echo "$AVAIL" | grep -qE "^${USER_MODEL}(:|$)"; then
    CHOSEN="$USER_MODEL"
fi

# Build /api/chat request
REQ=$(echo "$PAYLOAD" | python3 -c "
import sys, json
d = json.load(sys.stdin)
out = {
    'model': '$CHOSEN',
    'messages': d.get('messages', []),
    'stream': False,
    'options': {
        'num_predict': int(d.get('max_tokens', 1024)),
        'temperature': float(d.get('temperature', 0.7)),
        'top_p': float(d.get('top_p', 0.95)),
        'num_ctx': int(d.get('num_ctx', 8192)),
    }
}
print(json.dumps(out))
")

# POST to Ollama
RESP=$(curl -fsSm 300 "http://${OLLAMA_HOST}/api/chat" \
            -H "Content-Type: application/json" -d "$REQ" 2>/dev/null)
if [[ -z "$RESP" ]]; then
    echo "{\"error\":\"ollama call timed out\"}" >&2
    exit 1
fi

# Extract message content
echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
msg = d.get('message', {}).get('content', '')
if not msg:
    msg = d.get('response', '')
print(msg)
"
