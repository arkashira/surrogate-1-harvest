#!/usr/bin/env bash
# Surrogate-1 unified LLM ladder.
# Drop-in: any v2 script can pipe JSON in and get plain text out.
#
# Routing order (configurable via SURROGATE_LADDER env var):
#   1. zero-gpu       (own A10G Space, free PRO, ~5-10s cold start, 60-120s GPU)
#   2. cerebras       (wafer-scale, fastest)
#   3. groq           (LPU, fast)
#   4. hf-inference   (HF router, PRO 2.5x quota)
#   5. gemini         (Google AI Studio, 15 RPM free)
#   6. openrouter     (multi-provider router, 5-key fallback)
#   7. chutes         (varied providers)
#   8. nvidia         (NVIDIA NIM)
#   9. ollama         (local CPU, only if alive)
#
# Input (stdin, JSON):
#   {"system": "...", "prompt": "...", "max_tokens": N, "temperature": T}
# Output (stdout):
#   plain text response
#
# Usage:
#   echo '{"prompt":"hi","max_tokens":50}' | bash llm-ladder.sh
#   SURROGATE_LADDER="zero-gpu,cerebras,groq" bash llm-ladder.sh
set -u

LADDER="${SURROGATE_LADDER:-zero-gpu,cerebras,groq,hf-inference,gemini,openrouter,chutes,nvidia,ollama}"
# Prefer the bridge dir we live in (sibling files); fall back to ~/.surrogate/bin
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="${SURROGATE_BIN:-$SCRIPT_DIR}"
[[ ! -x "$BIN/cerebras-bridge.sh" ]] && BIN="$HOME/.surrogate/bin"
LOG="$HOME/.surrogate/logs/llm-ladder.log"
mkdir -p "$(dirname "$LOG")"

# Read JSON request from stdin
REQ=$(cat)
[[ -z "$REQ" ]] && { echo "llm-ladder: empty request" >&2; exit 2; }

PROMPT=$(echo "$REQ" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('prompt','') or d.get('text',''))")
SYS_PROMPT=$(echo "$REQ" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('system',''))")
MAX_TOKENS=$(echo "$REQ" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('max_tokens',1024))")
TEMP=$(echo "$REQ" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('temperature',0.4))")

# Combine system + prompt for bridges that take plain text
if [[ -n "$SYS_PROMPT" ]]; then
    FULL_PROMPT="${SYS_PROMPT}\n\n${PROMPT}"
else
    FULL_PROMPT="$PROMPT"
fi

[[ -z "$FULL_PROMPT" ]] && { echo "llm-ladder: no prompt" >&2; exit 2; }

IFS=',' read -ra TIERS <<< "$LADDER"
for tier in "${TIERS[@]}"; do
    tier=$(echo "$tier" | xargs)  # trim
    bridge="$BIN/${tier}-bridge.sh"
    [[ ! -x "$bridge" ]] && continue

    echo "[$(date '+%H:%M:%S')] try $tier" >> "$LOG"
    # Each bridge has a different arg set — only pass --max-tokens (universal).
    # Temperature handled per-bridge default; pass via env if absolutely needed.
    out=$(echo -e "$FULL_PROMPT" | bash "$bridge" --max-tokens "$MAX_TOKENS" 2>>"$LOG")
    rc=$?
    if [[ $rc -eq 0 && -n "$out" && ${#out} -gt 5 ]]; then
        echo "[$(date '+%H:%M:%S')] $tier OK ${#out}b" >> "$LOG"
        echo "$out"
        exit 0
    fi
    echo "[$(date '+%H:%M:%S')] $tier FAIL rc=$rc len=${#out}" >> "$LOG"
done

echo "llm-ladder: all tiers failed" >&2
exit 1
