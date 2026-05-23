#!/usr/bin/env bash
# Auto-upgrade surrogate-1 to Qwen3-Coder-30B-A3B base once Ollama pull completes.
# Runs every 5 min via cron. Exits quickly if already upgraded.
# Keeps current surrogate-1 (on qwen2.5-coder:7b) serving until new base is ready.
set -u

LOG="$HOME/.claude/logs/surrogate-auto-upgrade.log"
MODELFILE_NEW="$HOME/axentx/surrogate/Modelfile.surrogate-1-v2"
MARKER="$HOME/.claude/state/surrogate-upgraded-to-30b"
mkdir -p "$(dirname "$LOG")" "$(dirname "$MARKER")"

# Already upgraded?
[[ -f "$MARKER" ]] && exit 0

# Is qwen3-coder:30b available locally?
if ! /usr/local/bin/ollama list 2>/dev/null | grep -q "qwen3-coder:30b\b"; then
    echo "[$(date '+%H:%M:%S')] waiting — qwen3-coder:30b not pulled yet" >> "$LOG"
    exit 0
fi

# Rebuild surrogate-1 from 30b base
echo "[$(date '+%H:%M:%S')] 30b available — rebuilding surrogate-1" >> "$LOG"
/usr/local/bin/ollama create surrogate-1 -f "$MODELFILE_NEW" 2>&1 | tail -3 >> "$LOG"
RC=$?
if [[ $RC -eq 0 ]]; then
    echo "[$(date '+%H:%M:%S')] ✅ upgraded surrogate-1 → Qwen3-Coder-30B-A3B" >> "$LOG"
    touch "$MARKER"
    # Smoke test
    echo '{"model":"surrogate-1","messages":[{"role":"user","content":"say ok"}],"max_tokens":5,"stream":false}' | \
        curl -s -X POST http://localhost:11434/v1/chat/completions -d @- 2>&1 | \
        head -c 200 >> "$LOG"
    echo "" >> "$LOG"
else
    echo "[$(date '+%H:%M:%S')] ❌ rebuild failed rc=$RC" >> "$LOG"
fi
