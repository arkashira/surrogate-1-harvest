#!/usr/bin/env bash
# test-llm-fallback.sh — exercise call_llm() once per provider in the chain.
#
# Strategy: for each provider, blank out every OTHER provider's key, then call
# call_llm("ping") once. If it returns text, that provider answered. If all
# providers silently fail, the script reports "ALL FAILED".
#
# Usage:
#   bash bin/test-llm-fallback.sh
#
# Reads keys from the live environment (or from /etc/default/axentx-daemons
# if running on the systemd host). Does NOT mutate any state — it only tests
# isolation via subshell env scoping.

set -u

REPO="${REPO_ROOT:-/opt/surrogate-1-harvest}"
[ -d "$REPO" ] || REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Source axentx-daemons defaults if available so we get the same key set the
# daemons actually use. Stays no-op if the file is missing (local dev).
if [ -f /etc/default/axentx-daemons ]; then
  set -a; . /etc/default/axentx-daemons; set +a
fi

# Provider -> primary env-var name. Order MUST mirror call_llm chain order.
PROVIDERS=(
  "Groq:GROQ_API_KEY"
  "Cerebras:CEREBRAS_API_KEY"
  "SambaNova:SAMBANOVA_API_KEY"
  "NVIDIA-NIM:NVIDIA_NIM_API_KEY"
  "Kimi:KIMI_API_KEY"
  "xAI:GROK_API_KEY"
  "Chutes:CHUTES_API_KEY"
  "OpenRouter:OPENROUTER_API_KEY"
  "GitHub-Models:GITHUB_MODELS_TOKEN"
  "CloudflareWorkersAI:CLOUDFLARE_API_TOKEN"
  "Gemini:GOOGLE_API_KEY"
)

# All key names that call_llm consults — used to blank out the others.
ALL_KEYS=(
  GROQ_API_KEY CEREBRAS_API_KEY SAMBANOVA_API_KEY
  NVIDIA_NIM_API_KEY NVIDIA_API_KEY
  KIMI_API_KEY MOONSHOT_API_KEY
  GROK_API_KEY XAI_API_KEY
  CHUTES_API_KEY OPENROUTER_API_KEY GITHUB_MODELS_TOKEN
  CLOUDFLARE_API_TOKEN GOOGLE_API_KEY GEMINI_API_KEY
)

pass=0
fail=0
results=()

for entry in "${PROVIDERS[@]}"; do
  name="${entry%%:*}"
  primary_var="${entry##*:}"

  # Build env-isolation flags: only `primary_var` (and its aliases) keep their
  # value. Everything else gets force-empty so call_llm skips it.
  env_args=()
  for k in "${ALL_KEYS[@]}"; do
    case "$k" in
      "$primary_var") env_args+=("$k=${!k:-}") ;;
      # Honor recognized aliases per provider so the chain still hits the
      # right entry — call_llm reads either NVIDIA_NIM_API_KEY OR NVIDIA_API_KEY.
      NVIDIA_API_KEY)
        if [ "$primary_var" = "NVIDIA_NIM_API_KEY" ]; then
          env_args+=("$k=${!k:-}"); else env_args+=("$k="); fi ;;
      MOONSHOT_API_KEY)
        if [ "$primary_var" = "KIMI_API_KEY" ]; then
          env_args+=("$k=${!k:-}"); else env_args+=("$k="); fi ;;
      XAI_API_KEY)
        if [ "$primary_var" = "GROK_API_KEY" ]; then
          env_args+=("$k=${!k:-}"); else env_args+=("$k="); fi ;;
      GEMINI_API_KEY)
        if [ "$primary_var" = "GOOGLE_API_KEY" ]; then
          env_args+=("$k=${!k:-}"); else env_args+=("$k="); fi ;;
      *) env_args+=("$k=") ;;
    esac
  done
  # CF needs both token AND account id
  if [ "$primary_var" = "CLOUDFLARE_API_TOKEN" ]; then
    env_args+=("CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-}")
  else
    env_args+=("CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-}")
  fi
  env_args+=("USE_V1_FALLBACK=0")  # don't hit Surrogate-1 v1 during isolation tests

  # Skip provider with no key configured at all
  if [ -z "${!primary_var:-}" ]; then
    printf "%-22s SKIP (no key)\n" "$name"
    results+=("$name=SKIP")
    continue
  fi

  out=$(env -i PATH="$PATH" HOME="$HOME" "${env_args[@]}" \
    python3 -c "
import sys, os
sys.path.insert(0, '$REPO/bin')
from axentx_pipeline import call_llm
try:
    r = call_llm('Reply with the single word: PONG.', max_tokens=20, timeout=20)
    print('OK:' + (r or '').strip()[:80])
except Exception as e:
    print('FAIL:' + type(e).__name__ + ':' + str(e)[:120])
" 2>&1 | tail -1)

  if [[ "$out" == OK:* ]]; then
    printf "%-22s OK   %s\n" "$name" "${out#OK:}"
    pass=$((pass + 1))
    results+=("$name=OK")
  else
    printf "%-22s FAIL %s\n" "$name" "${out#FAIL:}"
    fail=$((fail + 1))
    results+=("$name=FAIL")
  fi
done

echo
echo "summary: $pass OK, $fail FAIL, $((${#PROVIDERS[@]} - pass - fail)) SKIP"
[ "$pass" -gt 0 ] || { echo "ALL FAILED — chain is unhealthy"; exit 1; }
