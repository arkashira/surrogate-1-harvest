#!/usr/bin/env bash
# Hermes start orchestrator for HF Space.
# Boots: persistent /data mount → Redis → Ollama → axentx repos → daemons → status server.
set -uo pipefail

LOG_DIR="${HOME}/.claude/logs"
mkdir -p "$LOG_DIR"
echo "[$(date +%H:%M:%S)] hermes-hf-space boot start" | tee "$LOG_DIR/boot.log"

# ── 1. Persistent data — symlink state dirs to /data (HF persistent mount) ──
DATA="/data"
if [[ -d "$DATA" ]] && [[ -w "$DATA" ]]; then
    mkdir -p "$DATA"/{state,workspace,memory,reflexion,projects,ollama,surrogate,index}
    # Symlink critical paths so DB/training/ChromaDB persist across rebuilds
    for src in \
        "${HOME}/.claude/state:${DATA}/state" \
        "${HOME}/.hermes/workspace:${DATA}/workspace" \
        "${HOME}/.surrogate:${DATA}/surrogate" \
        "${HOME}/.ollama:${DATA}/ollama"; do
        target="${src%%:*}"
        link="${src##*:}"
        mkdir -p "$(dirname "$target")"
        if [[ ! -L "$target" ]]; then
            rm -rf "$target" 2>/dev/null
            ln -sfn "$link" "$target"
        fi
    done
    echo "[$(date +%H:%M:%S)] persistent /data linked" >> "$LOG_DIR/boot.log"
else
    echo "[$(date +%H:%M:%S)] WARN: /data not writable — running ephemeral!" >> "$LOG_DIR/boot.log"
fi

# ── 2. Bind HF Space secrets → ~/.hermes/.env ───────────────────────────────
mkdir -p ~/.hermes
{
    echo "# Auto-generated from HF Space secrets at boot"
    for k in OPENROUTER_API_KEY GEMINI_API_KEY GEMINI_API_KEY_2 \
             GITHUB_TOKEN GITHUB_TOKEN_POOL DISCORD_BOT_TOKEN DISCORD_WEBHOOK \
             CEREBRAS_API_KEY GROQ_API_KEY SAMBANOVA_API_KEY \
             CLOUDFLARE_API_KEY NVIDIA_API_KEY CHUTES_API_KEY ANTHROPIC_API_KEY; do
        v="${!k:-}"
        [[ -n "$v" ]] && echo "${k}=${v}"
    done
} > ~/.hermes/.env
chmod 600 ~/.hermes/.env

# ── 3. Git config + clone axentx repos for auto-orchestrate auto-commit ────
GH_TOKEN=$(echo "${GITHUB_TOKEN_POOL:-}" | cut -d',' -f1)
if [[ -n "$GH_TOKEN" ]]; then
    git config --global user.email "hermes@axentx.ai"
    git config --global user.name  "Hermes (Surrogate-1)"
    git config --global init.defaultBranch main
    git config --global pull.rebase true
    git config --global push.default current

    PROJECTS_DIR="${DATA}/projects"
    mkdir -p "$PROJECTS_DIR"
    # Symlink to the path auto-orchestrate-loop expects
    rm -rf ~/axentx 2>/dev/null
    ln -sfn "$PROJECTS_DIR" ~/axentx

    # Clone axentx repos (skip if already exists)
    # Note: axiomops dropped (using arkship instead). arkship lives under arkashira org.
    for repo_spec in \
        "Costinel:AXENTX/Costinel" \
        "Vanguard:AXENTX/vanguard" \
        "arkship:arkashira/arkship" \
        "surrogate-1:AXENTX/surrogate-1"; do
        local_name="${repo_spec%%:*}"
        gh_path="${repo_spec##*:}"
        target="${PROJECTS_DIR}/${local_name}"
        if [[ ! -d "$target/.git" ]]; then
            echo "[$(date +%H:%M:%S)] cloning $gh_path..." >> "$LOG_DIR/boot.log"
            git clone "https://x-access-token:${GH_TOKEN}@github.com/${gh_path}.git" "$target" \
                >> "$LOG_DIR/git-clone.log" 2>&1 || \
                echo "[$(date +%H:%M:%S)] WARN: clone $gh_path failed" >> "$LOG_DIR/boot.log"
        else
            # Update existing checkout (pull latest before committing)
            (cd "$target" && git fetch && git pull --rebase 2>&1 | tail -3) \
                >> "$LOG_DIR/git-pull.log" 2>&1 || true
        fi
    done

    # Persist token for any push from auto-orchestrate
    git config --global credential.helper "store --file=$HOME/.git-credentials"
    echo "https://x-access-token:${GH_TOKEN}@github.com" > ~/.git-credentials
    chmod 600 ~/.git-credentials
    echo "[$(date +%H:%M:%S)] git auth configured + 4 axentx repos cloned" >> "$LOG_DIR/boot.log"
fi

# ── 4. Redis (TCP only) ─────────────────────────────────────────────────────
redis-server --daemonize yes --port 6379 --bind 127.0.0.1 \
    --maxmemory 1gb --maxmemory-policy allkeys-lru
sleep 1
redis-cli -h 127.0.0.1 -p 6379 ping >> "$LOG_DIR/redis.log" 2>&1

# ── 5. Ollama (background, CPU mode) ────────────────────────────────────────
OLLAMA_MODELS="${HOME}/.ollama/models" \
OLLAMA_HOST=127.0.0.1:11434 \
nohup ollama serve > "$LOG_DIR/ollama.log" 2>&1 &
sleep 6

# Pull model only on first boot (model cache lives in /data/.ollama/models)
if ! ollama list 2>/dev/null | grep -q "gemma4:e4b"; then
    echo "[$(date +%H:%M:%S)] pulling gemma4:e4b (~9.6 GB, first boot, 5-15 min)" >> "$LOG_DIR/boot.log"
    nohup ollama pull gemma4:e4b > "$LOG_DIR/ollama-pull.log" 2>&1 &
fi

# ── 6. Discord bot (background) ─────────────────────────────────────────────
if [[ -n "${DISCORD_BOT_TOKEN:-}" ]]; then
    set -a; source ~/.hermes/.env; set +a
    nohup python ~/.claude/bin/hermes-discord-bot.py >> "$LOG_DIR/discord-bot.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] discord bot started" >> "$LOG_DIR/boot.log"
fi

# ── 7. Cron loop — fires Hermes daemons 24/7 (no sleep gaps) ────────────────
cat > /tmp/hermes-cron.sh <<'CRONSH'
#!/bin/bash
set -a; source ~/.hermes/.env 2>/dev/null; set +a
LOG="${HOME}/.claude/logs/cron.log"
mkdir -p "$(dirname "$LOG")"
while true; do
    M=$(($(date +%s) / 60))
    # Every 90s: continuous local dev (gemma)
    [[ $((M % 2)) -eq 0 ]] && bash ~/.claude/bin/surrogate-dev-loop.sh 1 >> "$LOG" 2>&1 &
    # Every 5 min: producer pushes priorities to Redis
    [[ $((M % 5)) -eq 0 ]] && bash ~/.claude/bin/work-queue-producer.sh >> "$LOG" 2>&1 &
    # Every 20 min: full orchestrate chain (architect → dev → qa → reviewer + git push)
    [[ $((M % 20)) -eq 0 ]] && bash ~/.claude/bin/auto-orchestrate-loop.sh >> "$LOG" 2>&1 &
    # Every 30 min: scrape loop (parallel 4)
    [[ $((M % 30)) -eq 0 ]] && bash ~/.claude/bin/domain-scrape-loop.sh 1700 4 >> "$LOG" 2>&1 &
    # Every 60 min: keyword tuner
    [[ $((M % 60)) -eq 0 ]] && bash ~/.claude/bin/scrape-keyword-tuner.sh >> "$LOG" 2>&1 &
    sleep 60
done
CRONSH
chmod +x /tmp/hermes-cron.sh
nohup /tmp/hermes-cron.sh > "$LOG_DIR/cron-master.log" 2>&1 &
echo "[$(date +%H:%M:%S)] cron loop started" >> "$LOG_DIR/boot.log"

# ── 8. Status HTTP server on :7860 (FastAPI/uvicorn — robust binding) ──────
echo "[$(date +%H:%M:%S)] starting status server :7860" >> "$LOG_DIR/boot.log"

# Run as PID 1 — uvicorn handles signals + auto-restart on crash
exec python3 ~/.claude/bin/hermes-status-server.py >> "$LOG_DIR/status-server.log" 2>&1
