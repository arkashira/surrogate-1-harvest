#!/usr/bin/env bash
# Hermes start orchestrator for HF Space.
# Boots: persistent /data mount → Redis → Ollama → axentx repos → daemons → status server.
set -uo pipefail

LOG_DIR="${HOME}/.surrogate/logs"
mkdir -p "$LOG_DIR"
echo "[$(date +%H:%M:%S)] hermes-hf-space boot start"
echo "[$(date +%H:%M:%S)] hermes-hf-space boot start" >> "$LOG_DIR/boot.log"

# Trace mode for early steps only (no secrets here yet) — find hang point but stay safe
PS4='[trace ${LINENO}] '
set -x

# Echo stdout so HF run-logs see progress (safe steps before .env is loaded)
exec > >(tee -a "$LOG_DIR/boot.log") 2>&1

# ── 1. Persistent data — symlink state subdirs to /data (HF persistent mount) ──
# bin/ is NOT persisted (baked into image, refreshed on every push).
# Persisted: state (DBs), logs, memory, skills, sessions, training pairs,
#            workspace (hermes runtime), projects (axentx clones), ollama (model cache).
DATA="/data"
if [[ -d "$DATA" ]] && [[ -w "$DATA" ]]; then
    mkdir -p "$DATA"/{state,logs,memory,skills,sessions,workspace,projects,ollama,training,reflexion,index}
    # Migrate from any older layout (one-time): if /data/surrogate/state exists, move up one level
    if [[ -d "$DATA/surrogate/state" ]] && [[ ! -L "$DATA/state" ]]; then
        mv "$DATA/surrogate"/* "$DATA/" 2>/dev/null || true
        rmdir "$DATA/surrogate" 2>/dev/null || true
    fi

    for spec in \
        "${HOME}/.surrogate/state:${DATA}/state" \
        "${HOME}/.surrogate/logs:${DATA}/logs" \
        "${HOME}/.surrogate/memory:${DATA}/memory" \
        "${HOME}/.surrogate/skills:${DATA}/skills" \
        "${HOME}/.surrogate/sessions:${DATA}/sessions" \
        "${HOME}/.hermes/workspace:${DATA}/workspace" \
        "${HOME}/.ollama:${DATA}/ollama"; do
        target="${spec%%:*}"
        link="${spec##*:}"
        mkdir -p "$(dirname "$target")"
        if [[ ! -L "$target" ]]; then
            rm -rf "$target" 2>/dev/null
            ln -sfn "$link" "$target"
        fi
    done

    # training-pairs.jsonl — single file persistence
    if [[ ! -L "${HOME}/.surrogate/training-pairs.jsonl" ]]; then
        rm -f "${HOME}/.surrogate/training-pairs.jsonl" 2>/dev/null
        touch "${DATA}/training-pairs.jsonl"
        ln -sfn "${DATA}/training-pairs.jsonl" "${HOME}/.surrogate/training-pairs.jsonl"
    fi

    echo "[$(date +%H:%M:%S)] persistent /data linked (state, logs, memory, skills, sessions, workspace, ollama, training-pairs)" >> "$LOG_DIR/boot.log"
else
    echo "[$(date +%H:%M:%S)] WARN: /data not writable — running ephemeral!" >> "$LOG_DIR/boot.log"
fi

# ── 2. Bind HF Space secrets → ~/.hermes/.env ───────────────────────────────
# 🔒 DISABLE shell trace before touching secret values.
set +x
echo "[$(date +%H:%M:%S)] writing ~/.hermes/.env from secret env vars (trace OFF)"
mkdir -p ~/.hermes
{
    echo "# Auto-generated from HF Space secrets at boot"
    for k in OPENROUTER_API_KEY GEMINI_API_KEY GEMINI_API_KEY_2 \
             GITHUB_TOKEN GITHUB_TOKEN_POOL DISCORD_BOT_TOKEN DISCORD_WEBHOOK \
             CEREBRAS_API_KEY GROQ_API_KEY SAMBANOVA_API_KEY \
             CLOUDFLARE_API_KEY NVIDIA_API_KEY CHUTES_API_KEY ANTHROPIC_API_KEY \
             HF_TOKEN HUGGING_FACE_HUB_TOKEN; do
        v="${!k:-}"
        [[ -n "$v" ]] && echo "${k}=${v}"
    done
} > ~/.hermes/.env
chmod 600 ~/.hermes/.env
echo "[$(date +%H:%M:%S)] .env written ($(wc -l < ~/.hermes/.env) keys, perms 600)"
# Trace OFF for the rest of boot — we already have line numbers above and won't need them post-secrets.

# ── 3. Git config + clone axentx repos for auto-orchestrate auto-commit ────
# Disable interactive prompts globally so failed-auth git ops fail fast.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/true

GH_TOKEN=$(echo "${GITHUB_TOKEN_POOL:-}" | cut -d',' -f1)
if [[ -n "$GH_TOKEN" ]]; then
    git config --global user.email "hermes@axentx.ai"
    git config --global user.name  "Hermes (Surrogate-1)"
    git config --global init.defaultBranch main
    git config --global pull.rebase true
    git config --global push.default current

    PROJECTS_DIR="${DATA}/projects"
    mkdir -p "$PROJECTS_DIR"
    rm -rf ~/axentx 2>/dev/null
    ln -sfn "$PROJECTS_DIR" ~/axentx

    # Clone axentx repos in background with hard timeout — never blocks boot
    # Repos all live under github.com/arkashira/* (verified via api.github.com 2026-04-28)
    for repo_spec in \
        "Costinel:arkashira/Costinel" \
        "vanguard:arkashira/vanguard" \
        "arkship:arkashira/arkship" \
        "surrogate:arkashira/surrogate" \
        "workio:arkashira/workio" \
        "hermes-toolbelt:arkashira/hermes-toolbelt"; do
        local_name="${repo_spec%%:*}"
        gh_path="${repo_spec##*:}"
        target="${PROJECTS_DIR}/${local_name}"
        (
            if [[ ! -d "$target/.git" ]]; then
                echo "[$(date +%H:%M:%S)] cloning $gh_path..." >> "$LOG_DIR/boot.log"
                timeout 30 git clone --depth 50 \
                    "https://x-access-token:${GH_TOKEN}@github.com/${gh_path}.git" "$target" \
                    >> "$LOG_DIR/git-clone.log" 2>&1 || \
                    echo "[$(date +%H:%M:%S)] WARN: clone $gh_path failed/timeout" >> "$LOG_DIR/boot.log"
            else
                cd "$target" && timeout 20 git pull --rebase >> "$LOG_DIR/git-pull.log" 2>&1 || true
            fi
        ) &
    done
    # Don't wait — let clones finish in background while boot continues

    # Persist token for any push from auto-orchestrate
    git config --global credential.helper "store --file=$HOME/.git-credentials"
    echo "https://x-access-token:${GH_TOKEN}@github.com" > ~/.git-credentials
    chmod 600 ~/.git-credentials
    echo "[$(date +%H:%M:%S)] git auth configured + clone jobs spawned" >> "$LOG_DIR/boot.log"
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

# Pull models only on first boot (cache lives in /data/.ollama/models).
# Primary coding brain: qwen3-coder MoE (newest official Qwen coder; ~16GB Q4, 3B active = fast on CPU).
# Fallback: qwen2.5-coder:14b (proven). Light: gemma4:e4b (kept for quick triage).
#
# Note: user asked about "qwen3.6" — that's a community general-chat fine-tune,
# not coder-specialized. qwen3-coder is the official Qwen team flagship for SDLC tasks.
# SERIAL pulls — concurrent pulls saturate the 16GB CPU and stall everything else.
# Background single chained job, not a parallel storm.
(
    if ! ollama list 2>/dev/null | grep -q "nomic-embed-text"; then
        echo "[$(date +%H:%M:%S)] pulling nomic-embed-text (~270MB, fastest — RAG)" >> "$LOG_DIR/boot.log"
        ollama pull nomic-embed-text > "$LOG_DIR/ollama-pull-embed.log" 2>&1
    fi
    if ! ollama list 2>/dev/null | grep -q "qwen2.5-coder:14b"; then
        echo "[$(date +%H:%M:%S)] pulling qwen2.5-coder:14b (~9 GB, fallback brain)" >> "$LOG_DIR/boot.log"
        ollama pull qwen2.5-coder:14b-instruct-q4_K_M > "$LOG_DIR/ollama-pull-fallback.log" 2>&1
    fi
    if ! ollama list 2>/dev/null | grep -q "qwen3-coder"; then
        echo "[$(date +%H:%M:%S)] pulling qwen3-coder:30b-a3b (~16 GB MoE, primary brain)" >> "$LOG_DIR/boot.log"
        ollama pull qwen3-coder:30b-a3b-instruct-q4_K_M > "$LOG_DIR/ollama-pull-coder.log" 2>&1
    fi
    # Skip devstral + yi-coder for now — over budget on free 16GB instance.
    # Re-enable after upgrade to HF Pro tier (32GB+).
    echo "[$(date +%H:%M:%S)] all model pulls done (serial, no CPU storm)" >> "$LOG_DIR/boot.log"
) &

# ── 6. Discord bot (only if egress to discord.com is reachable) ────────────
# HF Spaces free tier may block egress to discord.com — bot would crash-loop.
# Pre-flight check: if discord.com unreachable, skip bot, use webhook-only.
if [[ -n "${DISCORD_BOT_TOKEN:-}" ]]; then
    if curl -sS -o /dev/null -w "%{http_code}" --max-time 6 https://discord.com 2>/dev/null | grep -qE "^(200|301|302|307|308)$"; then
        set -a; source ~/.hermes/.env 2>/dev/null; set +a
        nohup python ~/.surrogate/bin/hermes-discord-bot.py >> "$LOG_DIR/discord-bot.log" 2>&1 &
        echo "[$(date +%H:%M:%S)] discord bot started (gateway reachable)"
    else
        echo "[$(date +%H:%M:%S)] discord.com unreachable — skipping bot, using webhook-only" >> "$LOG_DIR/boot.log"
    fi
fi

# ── 7a. Continuous scrape daemon (parallel 8 workers, ~10s cool-down) ──────
cat > /tmp/scrape-daemon.sh <<'SCRAPESH'
#!/bin/bash
# 8 concurrent scrape workers, near-zero idle time.
set -a; source ~/.hermes/.env 2>/dev/null; set +a
LOG="${HOME}/.surrogate/logs/scrape-continuous.log"
mkdir -p "$(dirname "$LOG")"
while true; do
    START=$(date +%s)
    bash ~/.surrogate/bin/domain-scrape-loop.sh 1500 8 >> "$LOG" 2>&1
    DUR=$(( $(date +%s) - START ))
    # Tight cool-downs — cloud has unlimited bandwidth, only rate-limit concern
    if [[ $DUR -lt 30 ]]; then sleep 30          # queue likely exhausted, give it time
    elif [[ $DUR -lt 120 ]]; then sleep 15
    else sleep 5
    fi
done
SCRAPESH
chmod +x /tmp/scrape-daemon.sh
nohup /tmp/scrape-daemon.sh > "$LOG_DIR/scrape-daemon.log" 2>&1 &
echo "[$(date +%H:%M:%S)] continuous scrape daemon (parallel=8) started" >> "$LOG_DIR/boot.log"

# ── 7b. Agentic crawler (URL frontier + visited stamps + link discovery) ────
nohup bash ~/.surrogate/bin/agentic-crawler.sh 6 > "$LOG_DIR/agentic-crawler.log" 2>&1 &
echo "[$(date +%H:%M:%S)] agentic crawler started (parallel=6)" >> "$LOG_DIR/boot.log"

# ── 7c. Skill-synthesis daemon (extract patterns from cloned repos → skills) ─
nohup bash ~/.surrogate/bin/skill-synthesis-daemon.sh > "$LOG_DIR/skill-synthesis.log" 2>&1 &
echo "[$(date +%H:%M:%S)] skill-synthesis daemon started" >> "$LOG_DIR/boot.log"

# ── 7b. Cron loop — non-scrape daemons (scrape now runs continuously above) ─
cat > /tmp/hermes-cron.sh <<'CRONSH'
#!/bin/bash
set -a; source ~/.hermes/.env 2>/dev/null; set +a
LOG="${HOME}/.surrogate/logs/cron.log"
mkdir -p "$(dirname "$LOG")"
while true; do
    M=$(($(date +%s) / 60))
    # Every 2 min: continuous local dev (qwen3-coder when ready, else gemma)
    [[ $((M % 2)) -eq 0 ]] && bash ~/.surrogate/bin/surrogate-dev-loop.sh 1 >> "$LOG" 2>&1 &
    # Every 5 min: producer pushes priorities to Redis
    [[ $((M % 5)) -eq 0 ]] && bash ~/.surrogate/bin/work-queue-producer.sh >> "$LOG" 2>&1 &
    # Every 3 min: training-pair push to HF (drains ~/.surrogate/training-pairs.jsonl)
    [[ $((M % 3)) -eq 0 ]] && bash ~/.surrogate/bin/push-training-to-hf.sh >> "$LOG" 2>&1 &
    # Every 20 min: full orchestrate chain (architect → dev → qa → reviewer + git push)
    [[ $((M % 20)) -eq 0 ]] && bash ~/.surrogate/bin/auto-orchestrate-loop.sh >> "$LOG" 2>&1 &
    # Every 30 min: research-apply (pop queue → orchestrate → ship feature)
    [[ $((M % 30)) -eq 15 ]] && bash ~/.surrogate/bin/surrogate-research-apply.sh >> "$LOG" 2>&1 &
    # Every 60 min: keyword tuner (adapts scrape queue based on yields)
    [[ $((M % 60)) -eq 0 ]] && bash ~/.surrogate/bin/scrape-keyword-tuner.sh >> "$LOG" 2>&1 &
    # Every 6 hours: research-loop (discover new features from competitors/papers)
    [[ $((M % 360)) -eq 30 ]] && bash ~/.surrogate/bin/surrogate-research-loop.sh >> "$LOG" 2>&1 &
    # Every 12 hours: dataset enrich (pulls fresh public datasets, dedups, uploads to HF)
    [[ $((M % 720)) -eq 60 ]] && bash ~/.surrogate/bin/dataset-enrich.sh >> "$LOG" 2>&1 &
    # Every 15 min: self-ingest training-pairs into FTS index (closes the self-improvement loop)
    [[ $((M % 15)) -eq 0 ]] && bash ~/.surrogate/bin/surrogate-self-ingest.sh >> "$LOG" 2>&1 &
    # Every 30 min: synthetic data generation (REWORK→APPROVE DPO + distilabel rewrite)
    [[ $((M % 30)) -eq 7 ]] && bash ~/.surrogate/bin/synthetic-data-from-rework.sh >> "$LOG" 2>&1 &
    sleep 60
done
CRONSH
chmod +x /tmp/hermes-cron.sh
nohup /tmp/hermes-cron.sh > "$LOG_DIR/cron-master.log" 2>&1 &
echo "[$(date +%H:%M:%S)] cron loop started" >> "$LOG_DIR/boot.log"

# ── 8. Status HTTP server on :7860 (FastAPI/uvicorn — robust binding) ──────
set +x   # silence trace for clean uvicorn logs
echo "[$(date +%H:%M:%S)] starting status server :7860" | tee -a "$LOG_DIR/boot.log"

# Verify deps before exec — print what's missing rather than silent crash
python3 -c "import fastapi, uvicorn; print(f'  fastapi {fastapi.__version__} + uvicorn {uvicorn.__version__} ok')" || {
    echo "❌ fastapi/uvicorn not importable — falling back to plain http.server"
    exec python3 -m http.server 7860 --bind 0.0.0.0
}

# Run as PID 1 — uvicorn handles signals + auto-restart on crash
exec python3 ~/.surrogate/bin/hermes-status-server.py
