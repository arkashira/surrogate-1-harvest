#!/usr/bin/env bash
# Keep local LLMs warm in GPU memory — ping every ~4 min (before Ollama's 5-min default unload).
# M3 24GB holds qwen-coder:7b (4.9GB) + granite4:7b (4.5GB) simultaneously = ~10GB GPU.
# Runs via cron: */4 * * * *  → no idle unload, always hot for bursty workloads.
set -u
LOG="$HOME/.claude/logs/keep-alive-local.log"
mkdir -p "$(dirname "$LOG")"

# Tiny ping to each always-hot model. keep_alive=-1 pins indefinitely (until next ping or server restart).
for MODEL in "qwen2.5-coder:7b" "granite4:7b-a1b-h"; do
    RESP=$(/usr/bin/curl -sS --max-time 10 \
        http://localhost:11434/api/generate \
        -d "{\"model\":\"$MODEL\",\"prompt\":\"ok\",\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_predict\":1}}" \
        2>&1)
    if echo "$RESP" | grep -q '"response"'; then
        echo "[$(date '+%H:%M:%S')] $MODEL alive" >> "$LOG"
    else
        echo "[$(date '+%H:%M:%S')] $MODEL FAIL: $(echo "$RESP" | head -c 100)" >> "$LOG"
    fi
done
