#!/usr/bin/env bash
# AI Fallback Chain (cost-optimized, cloud-only, no local LLM)
#
# Priority chain:
#   1. Claude Opus 4.7   via Max subscription  (primary, flat $100/mo)
#   2. Claude Sonnet 4.6 via Max subscription  (separate quota pool!)
#   3. OpenRouter        pay-per-use           (cheap+capable non-Sonnet picks)
#   4. Gemini 2.5 FL     FREE 1000/day
#   5. Groq Llama-3.3    FREE 1000/day
#
# Usage:
#   ai-fallback.sh "your question"
#   ai-fallback.sh --force gpt5 "your question"
#   ai-fallback.sh --tier cheap "your question"     # OpenRouter uses DeepSeek
#   ai-fallback.sh --skip claude-opus "your question"
set -e

# Source API keys FIRST — load BOTH env files (hermes + claude).
# Order matters: claude.env first, hermes.env wins on conflict
# (hermes has newer keys like GITHUB_MODELS_TOKEN, SAMBANOVA_API_KEY, CLOUDFLARE_*)
# shellcheck disable=SC1090
set -a
[ -f "$HOME/.surrogate/.env" ] && . "$HOME/.surrogate/.env"
[ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
set +a

QUERY=""
FORCE=""
SKIP=""
VERBOSE=0
TASK=""
export OR_TIER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --force)    FORCE="$2"; shift 2 ;;
    --skip)     SKIP="$2"; shift 2 ;;
    --tier)     export OR_TIER="$2"; shift 2 ;;
    --task)     TASK="$2"; shift 2 ;;
    --cheap)    export OR_TIER="cheap"; shift ;;
    --fast)     export OR_TIER="fast"; shift ;;
    --balanced) export OR_TIER="balanced"; shift ;;
    --premium)  export OR_TIER="premium"; shift ;;
    -v|--verbose) VERBOSE=1; shift ;;
    *)          QUERY="$QUERY $1"; shift ;;
  esac
done
QUERY=$(echo "$QUERY" | sed 's/^ *//')
[ -z "$QUERY" ] && { head -15 "$0"; exit 1; }

# --task <type> — pick the strongest free model per provider for the task.
# Sets per-provider env vars that try_* functions read (bridge --model alias).
# Auto-detect if not provided: code keywords → coding, reasoning keywords → reasoning.
if [ -z "$TASK" ]; then
  q_lower=$(echo "$QUERY" | /usr/bin/tr '[:upper:]' '[:lower:]')
  if echo "$q_lower" | grep -qE "code|function|implement|refactor|bug|class|method|api|sql|terraform|cloudformation|dockerfile|kubernetes|yaml|typescript|javascript|python|rust|golang"; then
    TASK="coding"
  elif echo "$q_lower" | grep -qE "analyze|reason|explain why|compare|evaluate|architect|design|trade-?off|deep|think step|proof|calculate|complex"; then
    TASK="reasoning"
  fi
fi

case "$TASK" in
  coding)
    # Code = Codestral (GitHub, Mistral) / DeepSeek-V3.1 (SambaNova) / Qwen Coder (local)
    export GITHUB_MODEL="codestral"     ; export SAMBANOVA_MODEL="deepseek"
    export CLOUDFLARE_MODEL="deepseek"  ; export GROQ_MODEL="qwen"
    export LOCAL_MODEL="qwen-coder"
    ;;
  reasoning)
    # Reasoning = DeepSeek R1 (GitHub, <think> CoT) / Grok 3 / DeepSeek R1 distill (CF)
    export GITHUB_MODEL="reasoning"     ; export SAMBANOVA_MODEL="deepseek-latest"
    export CLOUDFLARE_MODEL="reasoning" ; export GROQ_MODEL="qwen"
    export LOCAL_MODEL="granite"
    ;;
  fast)
    # Fast = smallest/quickest tier per provider
    export GITHUB_MODEL="mini"          ; export SAMBANOVA_MODEL="fast"
    export CLOUDFLARE_MODEL="fast"      ; export GROQ_MODEL="fast"
    export LOCAL_MODEL="tiny"
    ;;
  long-context|long|kimi)
    # 200k+ context — Kimi on CF, gpt-oss-120b elsewhere
    export GITHUB_MODEL="llama405"      ; export SAMBANOVA_MODEL="gpt-oss"
    export CLOUDFLARE_MODEL="kimi"      ; export GROQ_MODEL="gpt-oss"
    export LOCAL_MODEL="granite"
    ;;
  creative|chat|*)
    # Default — smartest general-purpose free model per provider
    export GITHUB_MODEL="gpt-4o"        ; export SAMBANOVA_MODEL="llama70"
    export CLOUDFLARE_MODEL="gpt-oss"   ; export GROQ_MODEL="llama70"
    export LOCAL_MODEL="granite"
    ;;
esac

# --- Semantic RAG context injection (embedding-powered) ---
# For coding/reasoning/creative tasks, fetch top-3 semantically similar docs
# from embeddings.db and prepend to QUERY. ~50ms overhead, improves grounding.
if [[ "$TASK" == "coding" || "$TASK" == "reasoning" || "$TASK" == "creative" ]]; then
    if [[ -f "$HOME/.surrogate/embeddings.db" ]]; then
        EMB_COUNT=$(/usr/bin/sqlite3 "$HOME/.surrogate/embeddings.db" 'SELECT COUNT(*) FROM embeddings' 2>/dev/null || echo 0)
        if [[ "$EMB_COUNT" -ge 100 ]]; then
            SEM_CONTEXT=$(python3 "$HOME/.surrogate/bin/embed-doc.py" --query "$QUERY" 2>/dev/null | head -15)
            if [[ -n "$SEM_CONTEXT" ]]; then
                QUERY="=== RAG CONTEXT (top-5 semantic matches from knowledge base) ===
$SEM_CONTEXT

=== TASK ===
$QUERY"
            fi
        fi
    fi
fi

log() { [ $VERBOSE -eq 1 ] && echo "[$(date +%H:%M:%S)] $*" >&2; }

# Capture successful response → log to knowledge base (non-blocking)
save_response() {
  local provider="$1" model="$2" response="$3"
  [ -z "$response" ] && return
  ( "$HOME/.surrogate/bin/log-interaction.sh" "$QUERY" "$response" "$provider" "$model" > /dev/null 2>&1 & ) || true
}

# --- System prompt from knowledge base + auto code-search if code query ---
build_system_prompt() {
  local kb="" profile="" code_ctx="" q_lower
  [ -f "$HOME/.surrogate/memory/knowledge_index.md" ] && kb="$(head -50 $HOME/.surrogate/memory/knowledge_index.md)"
  [ -f "$HOME/.surrogate/memory/user_profile.md" ] && profile="$(cat $HOME/.surrogate/memory/user_profile.md)"

  q_lower=$(echo "$QUERY" | /usr/bin/tr '[:upper:]' '[:lower:]')
  local is_generate=0 is_code=0
  echo "$q_lower" | grep -qE "code|function|implement|refactor|bug|error|class|method|api|endpoint|schema|model|service|controller|middleware|auth|database|query|sql|deploy|pipeline|terraform|cloudformation|dockerfile|kubernetes|helm|yaml" && is_code=1
  echo "$q_lower" | grep -qE "create|generate|write|build|new|template|scaffold|design" && is_generate=1

  if [ "$is_code" = "1" ] && [ -d "$HOME/.surrogate/code-vector-db" ]; then
    if [ "$is_generate" = "1" ] && [ -x "$HOME/.surrogate/bin/find-gold-examples.sh" ]; then
      # Generation task → inject FULL reference files (better style match)
      code_ctx=$("$HOME/.surrogate/bin/find-gold-examples.sh" --top 2 --max-bytes 5000 "$QUERY" 2>/dev/null)
    elif [ -x "$HOME/.surrogate/bin/code-search.sh" ]; then
      # Query task → snippets only (faster)
      code_ctx=$("$HOME/.surrogate/bin/code-search.sh" --top 3 "$QUERY" 2>/dev/null | head -60)
    fi
  fi

  local prompt="You are Ashira's AI assistant. Context: $profile

Pattern index: $kb"
  if [ -n "$code_ctx" ]; then
    prompt="$prompt

=== ASHIRA'S EXISTING CODE (match this style EXACTLY) ===
$code_ctx
=== END EXAMPLES ===

Style rules enforced:
- Follow naming/indent/comment style from examples above
- Use exact same Parameter/Resource names when applicable
- Preserve existing conventions (tags, naming, Description format)"
  fi
  prompt="$prompt

Be concise. Cite file paths when referencing existing code."
  echo "$prompt"
}
SYSTEM=$(build_system_prompt)

# --- Anthropic via Max plan (routes through claude-bridge.sh CLI) ---
# Direct HTTPS to api.anthropic.com with OAuth token returns 401 — OAuth flow
# is managed by `claude` CLI (keychain/config). Use the bridge instead.
try_anthropic() {
  local model="$1" extra="$2"
  log "→ Claude Max: $model"
  local out
  out=$(echo "$QUERY" | "$HOME/.surrogate/bin/claude-bridge.sh" --model "$model" $extra 2>>/tmp/ai-fallback.err) || return 1
  [ -z "$out" ] && return 1
  echo "$out"
  save_response "anthropic" "$model" "$out"
  return 0
}

# Opus needs --force outside 01:00-06:00 window; sonnet is always available
try_claude_opus()   { try_anthropic "opus" "--force"; }
try_claude_sonnet() { try_anthropic "sonnet" ""; }

# OpenRouter FREE — tries multiple free models (each has strict rate limit)
# Order: coder-first → general-powerhouse → smaller fallbacks
try_openrouter_free() {
  [ -z "${OPENROUTER_API_KEY:-}" ] && return 2
  local free_models=(
    "qwen/qwen3-coder:free"
    "qwen/qwen3-next-80b-a3b-instruct:free"
    "openai/gpt-oss-120b:free"
    "nvidia/nemotron-3-super-120b-a12b:free"
    "meta-llama/llama-3.3-70b-instruct:free"
    "z-ai/glm-4.5-air:free"
    "google/gemma-4-31b-it:free"
    "openai/gpt-oss-20b:free"
  )
  for m in "${free_models[@]}"; do
    OPENROUTER_MODEL="$m" try_openrouter && return 0
    log "  ↳ free '$m' unavailable, trying next free..."
  done
  return 1
}

# --- OpenRouter (cheap+capable non-Sonnet picks) ---
try_openrouter() {
  [ -z "${OPENROUTER_API_KEY:-}" ] && return 2
  # Default: GPT-5.4 (beats Claude Opus 4.6 per benchmarks, -50% cost vs Opus 4.7)
  local model="${OPENROUTER_MODEL:-openai/gpt-5.4}"
  case "${OR_TIER:-}" in
    # PAID tiers
    cheap)     model="deepseek/deepseek-v3.2" ;;       # $0.26/$0.42 — cheapest capable
    fast)      model="x-ai/grok-4.1-fast" ;;           # $0.20/$0.50 — ultra cheap, 2M ctx
    balanced)  model="openai/gpt-5.4" ;;               # $2.50/$15 — DEFAULT, beats Opus 4.6
    premium)   model="anthropic/claude-opus-4.7" ;;    # $5/$25 — if really need Opus
    grok)      model="x-ai/grok-4.20" ;;               # $2/$6 — 2M ctx, cool
    gemini)    model="google/gemini-3.1-pro-preview" ;;# $2/$12
    # FREE tiers (29 models available)
    free|free-coder) model="qwen/qwen3-coder:free" ;;  # coding, 262k ctx
    free-large)  model="qwen/qwen3-next-80b-a3b-instruct:free" ;; # 80B MoE
    free-nvidia) model="nvidia/nemotron-3-super-120b-a12b:free" ;; # 120B
    free-gptoss) model="openai/gpt-oss-120b:free" ;;   # OpenAI open-sourced
    free-llama)  model="meta-llama/llama-3.3-70b-instruct:free" ;;
    free-kimi)   model="moonshotai/kimi-k2.5" ;;       # Kimi 256k ctx
    free-glm)    model="z-ai/glm-4.5-air:free" ;;
    free-gemma)  model="google/gemma-4-31b-it:free" ;; # Google Gemma 4
  esac
  log "→ OpenRouter: $model"
  local body
  # Use env vars — avoids quote-escape hell with multiline system prompt.
  # max_tokens=4000 (GPT-5.4 requires >= 16; stay well above)
  body=$(ORM="$model" SYS="$SYSTEM" Q="$QUERY" "$HOME/.surrogate/venv/bin/python" -c "
import json, os
m = {'model':os.environ['ORM'],'max_tokens':4000,
     'messages':[{'role':'system','content':os.environ['SYS']},
                 {'role':'user','content':os.environ['Q']}]}
print(json.dumps(m))
" 2>&1) || { log "  body-build failed: $body"; return 1; }
  local resp code body_resp
  resp=$(curl -sS -w "\n%{http_code}" \
    --max-time 90 \
    -X POST "https://openrouter.ai/api/v1/chat/completions" \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" \
    -H "HTTP-Referer: https://ashira.local" \
    -H "X-Title: ai-fallback" \
    -H "content-type: application/json" \
    -d "$body" 2>&1)
  code=$(echo "$resp" | tail -1)
  body_resp=$(echo "$resp" | sed '$d')
  if [ "$code" != "200" ]; then
    # Log real error reason for debug
    local errmsg
    errmsg=$(echo "$body_resp" | "$HOME/.surrogate/venv/bin/python" -c "
import sys, json
try: d=json.load(sys.stdin); print(d.get('error',{}).get('message','unknown')[:120])
except: print('parse-fail')
" 2>/dev/null || echo "unknown")
    log "  [$code] $errmsg — falling through"
    return 1
  fi
  local out
  out=$(echo "$body_resp" | "$HOME/.surrogate/venv/bin/python" -c "
import sys, json
d = json.load(sys.stdin)
print(d['choices'][0]['message']['content'])
") || return 1
  echo "$out"
  save_response "openrouter" "$model" "$out"
  return 0
}

# --- Gemini (free) ---
try_gemini() {
  [ -z "${GEMINI_API_KEY:-}" ] && return 2
  local model="${GEMINI_MODEL:-gemini-2.5-flash}"
  log "→ Gemini: $model (free)"
  local body
  body=$("$HOME/.surrogate/venv/bin/python" -c "
import json
m = {'systemInstruction':{'parts':[{'text':'''$SYSTEM'''}]},
     'contents':[{'role':'user','parts':[{'text':'''$QUERY'''}]}],
     'generationConfig':{'maxOutputTokens':4000}}
print(json.dumps(m))
" 2>/dev/null)
  local resp code body_resp
  resp=$(curl -sS -w "\n%{http_code}" \
    -X POST "https://generativelanguage.googleapis.com/v1beta/models/$model:generateContent?key=$GEMINI_API_KEY" \
    -H "content-type: application/json" -d "$body" 2>&1)
  code=$(echo "$resp" | tail -1)
  body_resp=$(echo "$resp" | sed '$d')
  [ "$code" != "200" ] && { log "  [$code] falling through"; return 1; }
  local out
  out=$(echo "$body_resp" | "$HOME/.surrogate/venv/bin/python" -c "
import sys, json
d = json.load(sys.stdin)
print(d['candidates'][0]['content']['parts'][0]['text'])
") || return 1
  echo "$out"
  save_response "gemini" "$model" "$out"
  return 0
}

# --- Groq (free, ultra-fast) ---
try_groq() {
  [ -z "${GROQ_API_KEY:-}" ] && return 2
  local model="${GROQ_MODEL:-llama70}"
  log "→ Groq: $model (free)"
  # Route through groq-bridge for consistent alias handling (llama70, fast, qwen, gpt-oss...)
  local out
  out=$(echo "$QUERY" | "$HOME/.surrogate/bin/groq-bridge.sh" --model "$model" 2>>/tmp/ai-fallback.err) || return 1
  [ -z "$out" ] && return 1
  echo "$out"
  save_response "groq" "$model" "$out"
  return 0
}

# --- GitHub Models (free via PAT, OpenAI-compat, GPT-4o-mini/Llama 3.3/Mistral/DeepSeek) ---
try_github() {
  [ -z "${GITHUB_MODELS_TOKEN:-}${GITHUB_TOKEN:-}" ] && return 2
  local model="${GITHUB_MODEL:-gpt-4o}"
  log "→ GitHub Models: $model (free)"
  local out
  out=$(echo "$QUERY" | "$HOME/.surrogate/bin/github-bridge.sh" --model "$model" 2>>/tmp/ai-fallback.err) || return 1
  [ -z "$out" ] && return 1
  echo "$out"
  save_response "github" "$model" "$out"
  return 0
}

# --- SambaNova Cloud (free, ~500 tok/s Llama 3.3 70B / DeepSeek V3.2 / Llama 4) ---
try_sambanova() {
  [ -z "${SAMBANOVA_API_KEY:-}" ] && return 2
  local model="${SAMBANOVA_MODEL:-llama70}"
  log "→ SambaNova: $model (free)"
  local out
  out=$(echo "$QUERY" | "$HOME/.surrogate/bin/sambanova-bridge.sh" --model "$model" 2>>/tmp/ai-fallback.err) || return 1
  [ -z "$out" ] && return 1
  echo "$out"
  save_response "sambanova" "$model" "$out"
  return 0
}

# --- Cloudflare Workers AI (free 10k neurons/day, Llama 3.3 / Gemma-3 / Qwen Coder) ---
try_cloudflare() {
  [ -z "${CLOUDFLARE_API_TOKEN:-}${CF_API_TOKEN:-}" ] && return 2
  [ -z "${CLOUDFLARE_ACCOUNT_ID:-}${CF_ACCOUNT_ID:-}" ] && return 2
  local model="${CLOUDFLARE_MODEL:-gpt-oss}"
  log "→ Cloudflare WAI: $model (free)"
  local out
  out=$(echo "$QUERY" | "$HOME/.surrogate/bin/cloudflare-bridge.sh" --model "$model" 2>>/tmp/ai-fallback.err) || return 1
  [ -z "$out" ] && return 1
  echo "$out"
  save_response "cloudflare" "$model" "$out"
  return 0
}

# --- Local Ollama — always-on, always-free ultimate fallback ---
# Bench (M3 24GB): granite4:7b-a1b-h (4.2GB, ~7s/fib+memo — fast & correct).
# Task-aware: code → qwen-coder:7b, chat → granite, tiny → qwen:3b.
# gemma4:26b BLOCKED — user directive (too slow for this hw).
try_granite() {
  # Check ollama running
  curl -sS --max-time 3 http://localhost:11434/api/tags > /dev/null 2>&1 || return 2
  local alias="${LOCAL_MODEL:-granite}"
  log "→ Local Ollama: $alias (free, always-on)"
  local out
  out=$(echo "$QUERY" | "$HOME/.surrogate/bin/granite-bridge.sh" --model "$alias" 2>>/tmp/ai-fallback.err) || return 1
  [ -z "$out" ] && return 1
  echo "$out"
  save_response "ollama-local" "$alias" "$out"
  return 0
}

# --- Execute chain (FREE-FIRST for routine/bulk tasks) ---
# Order: free APIs → claude-sonnet (Max plan safety net) → local Ollama (ultimate backstop)
# IMPORTANT-tasks (retro/sprint/skill-sanitize/agent-critic/security-audit/mythos-audit)
#   → call claude-bridge.sh --model opus --force DIRECTLY, bypass this chain
# REVIEWER/hallucination-check → call claude-bridge.sh --model sonnet DIRECTLY
# Paid OpenRouter removed per user direction (use Max plan instead of pay-per-use)
PROVIDERS="github sambanova cloudflare groq openrouter-free gemini claude-sonnet granite"

# Explicit --force
if [ -n "$FORCE" ]; then
  case "$FORCE" in
    claude-opus|opus)    try_claude_opus   && exit 0 ;;
    claude-sonnet|sonnet) try_claude_sonnet && exit 0 ;;
    openrouter|or)       try_openrouter    && exit 0 ;;
    openrouter-free|free) try_openrouter_free && exit 0 ;;
    gpt5|gpt)            OPENROUTER_MODEL="openai/gpt-5.4" try_openrouter && exit 0 ;;
    grok)                OPENROUTER_MODEL="x-ai/grok-4.20" try_openrouter && exit 0 ;;
    deepseek)            OPENROUTER_MODEL="deepseek/deepseek-v3.2" try_openrouter && exit 0 ;;
    gemini)              try_gemini        && exit 0 ;;
    groq)                try_groq          && exit 0 ;;
    github|gh)           try_github        && exit 0 ;;
    sambanova|samba)     try_sambanova     && exit 0 ;;
    cloudflare|cf)       try_cloudflare    && exit 0 ;;
    granite|local|ollama) try_granite       && exit 0 ;;
    *)                   echo "[error] unknown --force '$FORCE'" >&2; exit 1 ;;
  esac
  echo "[error] forced provider failed" >&2; exit 1
fi

# Auto chain with skip support
for p in $PROVIDERS; do
  if [ -n "$SKIP" ] && [ "$p" = "$SKIP" ]; then continue; fi
  case "$p" in
    github)          try_github          && exit 0 ;;
    sambanova)       try_sambanova       && exit 0 ;;
    cloudflare)      try_cloudflare      && exit 0 ;;
    claude-opus)     try_claude_opus     && exit 0 ;;
    claude-sonnet)   try_claude_sonnet   && exit 0 ;;
    openrouter)      try_openrouter      && exit 0 ;;
    openrouter-free) try_openrouter_free && exit 0 ;;
    gemini)          try_gemini          && exit 0 ;;
    groq)            try_groq            && exit 0 ;;
    granite)         try_granite         && exit 0 ;;
  esac
done

echo "[error] all providers exhausted" >&2
exit 1
