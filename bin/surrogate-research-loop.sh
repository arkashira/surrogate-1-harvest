#!/usr/bin/env bash
# Surrogate Continuous Research Loop — runs every 6 hours, finds new AI agent features
# from GitHub trending, arxiv, blog releases, and writes findings to ~/.hermes/workspace/research/
#
# Auto-applies easy wins (slash commands, prompts) and queues complex ones for human review.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/surrogate-research-loop.log"
RESEARCH_DIR="$HOME/.hermes/workspace/research"
APPLIED_DIR="$RESEARCH_DIR/applied"
mkdir -p "$RESEARCH_DIR" "$APPLIED_DIR" "$(dirname "$LOG")"

CYCLE_TS=$(date +%Y%m%d_%H%M)
echo "[$(date +%H:%M:%S)] research cycle start ($CYCLE_TS)" | tee -a "$LOG"

# ── Resource guard: 20% headroom rule ────────────────────────────────────────
LOAD=$(uptime | awk -F'load averages:' '{print $2}' | awk '{print int($1)}')
if [[ $LOAD -gt 8 ]]; then
    echo "  resource-pause: load=$LOAD" | tee -a "$LOG"
    exit 0
fi

# ── Pick a research focus this cycle (round-robin via cycle counter) ────────
FOCUSES=(
    "github-trending-coding-agents"
    "arxiv-agentic-coding-this-week"
    "mcp-server-registry-new-additions"
    "claude-code-feature-updates"
    "cursor-cline-aider-changelog-this-week"
    "devsecops-agent-tools-new-2026"
    "thai-tech-blog-ai-coding-news"
    "huggingface-models-coding-released-7d"
    "self-improving-agent-research-papers"
    "code-review-bot-features"
)

CYCLE_FILE="$RESEARCH_DIR/.cycle-counter"
N=$(cat "$CYCLE_FILE" 2>/dev/null || echo 0)
FOCUS="${FOCUSES[$((N % ${#FOCUSES[@]}))]}"
echo $((N + 1)) > "$CYCLE_FILE"

OUT="$RESEARCH_DIR/cycle-${CYCLE_TS}-${FOCUS}.md"
echo "  focus: $FOCUS → $OUT" | tee -a "$LOG"

# ── Build prompt for surrogate CLI to research ─────────────────────────────
PROMPT="You are a research agent. Find NEW developments in: \"$FOCUS\" (last 7 days).

For each finding:
1. Source URL (GitHub repo / arxiv / blog)
2. What's new (1-2 sentences)
3. Why it matters for Surrogate-1 (DevSecOps AI coding agent)
4. Difficulty to integrate (1-5)
5. Concrete code/prompt/file to port if applicable

Use web_fetch + web_search + rag_query tools as needed.

Output to file ${OUT} with markdown structure:
- ## Top finds (5-10 items)
- ## Quick wins to apply (1-3 items, difficulty 1-2)
- ## Defer to human review (anything difficulty 4-5)

Then write a 1-line action TODO to ${RESEARCH_DIR}/queue.txt for each quick-win, format:
'apply <short description> | <file path> | <patch summary>'

Be selective — quality > quantity."

# ── Run research via cloud LLM API directly ────────────────────────────────
# Original $HOME/.local/bin/surrogate CLI was never installed on this Space,
# so every cycle was failing silently in 0s. Replaced with direct calls to
# whichever cloud LLM key is set (Cerebras → Groq → OpenRouter) with
# automatic fallback if a backend is rate-limited or unavailable.
START=$(date +%s)
RESEARCH_RESPONSE=""
for backend in cerebras groq openrouter; do
    case "$backend" in
        cerebras)   url="https://api.cerebras.ai/v1/chat/completions";    key="${CEREBRAS_API_KEY:-}";   model="qwen-3-coder-480b" ;;
        groq)       url="https://api.groq.com/openai/v1/chat/completions"; key="${GROQ_API_KEY:-}";       model="qwen/qwen3-32b" ;;
        openrouter) url="https://openrouter.ai/api/v1/chat/completions";   key="${OPENROUTER_API_KEY:-}"; model="qwen/qwen3-coder:free" ;;
    esac
    [[ -z "$key" ]] && { echo "  [$backend] no key — skip" >> "$LOG"; continue; }
    RESEARCH_RESPONSE=$(curl -sS --max-time 90 "$url" \
        -H "Authorization: Bearer $key" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c "import json,sys; print(json.dumps({'model':sys.argv[1],'messages':[{'role':'user','content':sys.argv[2]}],'max_tokens':4000,'temperature':0.4}))" "$model" "$PROMPT")" 2>>"$LOG" \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('choices',[{}])[0].get('message',{}).get('content',''))" 2>>"$LOG" || true)
    if [[ -n "$RESEARCH_RESPONSE" ]]; then
        echo "  [$backend] response: $(echo "$RESEARCH_RESPONSE" | wc -c) chars" >> "$LOG"
        break
    fi
    echo "  [$backend] empty/error — try next" >> "$LOG"
done

if [[ -n "$RESEARCH_RESPONSE" ]]; then
    {
        echo "# Research cycle: $FOCUS ($CYCLE_TS)"
        echo ""
        echo "$RESEARCH_RESPONSE"
    } > "$OUT"
    # Extract any 'apply ...' lines into the queue
    echo "$RESEARCH_RESPONSE" | grep -E "^apply " >> "$RESEARCH_DIR/queue.txt" 2>/dev/null || true
fi

DUR=$(( $(date +%s) - START ))
echo "[$(date +%H:%M:%S)] research done in ${DUR}s" | tee -a "$LOG"

# ── Discord notify if new findings worth attention ─────────────────────────
if [[ -f "$OUT" ]] && [[ -s "$OUT" ]]; then
    QUICK_WINS=$(grep -c "^apply " "$RESEARCH_DIR/queue.txt" 2>/dev/null || echo 0)
    [[ -x "$HOME/.local/bin/notify-discord.sh" ]] && \
        "$HOME/.local/bin/notify-discord.sh" info "🔬 Research cycle done" \
        "Focus: $FOCUS · ${DUR}s · $(wc -l < "$OUT") lines · $QUICK_WINS quick-wins queued" 2>/dev/null || true
fi

echo "[$(date +%H:%M:%S)] cycle done" | tee -a "$LOG"
