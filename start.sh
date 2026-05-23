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
        # Always ensure backing directory exists + writable. If the persistent
        # /data mount becomes unavailable mid-run, daemon writes to symlinked
        # path fail with Errno 5 I/O error (audit 2026-04-29). Recreating the
        # link defensively each boot fixes stale-symlink cases.
        mkdir -p "$link" 2>/dev/null || true
        if [[ ! -L "$target" ]] || [[ ! -d "$target/" ]]; then
            # Either not-a-symlink OR broken symlink (target unreachable)
            rm -rf "$target" 2>/dev/null
            ln -sfn "$link" "$target"
        fi
        # Final sanity probe — write a marker; if it fails, the persistent
        # mount is broken regardless of the symlink, so log loudly.
        if ! touch "$target/.boot-marker" 2>/dev/null; then
            echo "[$(date +%H:%M:%S)] ⚠ FATAL: $target/ not writable — daemon log writes will Errno 5"
        fi
    done

    # training-pairs.jsonl — single file persistence
    if [[ ! -L "${HOME}/.surrogate/training-pairs.jsonl" ]]; then
        rm -f "${HOME}/.surrogate/training-pairs.jsonl" 2>/dev/null
        touch "${DATA}/training-pairs.jsonl"
        ln -sfn "${DATA}/training-pairs.jsonl" "${HOME}/.surrogate/training-pairs.jsonl"
    fi

    # ── One-time offset reset: skip polluted agentic-crawler placeholder backlog ──
    if [[ ! -f "${HOME}/.surrogate/.offset-reset-done" ]] && [[ -f "${HOME}/.surrogate/training-pairs.jsonl" ]]; then
        CUR=$(wc -l < "${HOME}/.surrogate/training-pairs.jsonl" | tr -d ' ')
        echo "$CUR" > "${HOME}/.surrogate/.training-push-offset"
        echo "$CUR" > "${HOME}/.surrogate/.self-ingest-offset"
        touch "${HOME}/.surrogate/.offset-reset-done"
        echo "[$(date +%H:%M:%S)] one-time offset reset → $CUR (skip placeholder backlog)" >> "$LOG_DIR/boot.log"
    fi

    # ── Boot-time dedup.db corruption check ──────────────────────────────
    # 16 parallel shards previously corrupted the SQLite WAL. If the DB is
    # unreadable on boot, back it up and force re-bootstrap from scratch.
    DEDUP_DB="${HOME}/.surrogate/state/dedup.db"
    if [[ -f "$DEDUP_DB" ]]; then
        if ! sqlite3 "$DEDUP_DB" "SELECT 1 FROM seen_hashes LIMIT 1" >/dev/null 2>&1; then
            TS=$(date +%s)
            mv "$DEDUP_DB" "${DEDUP_DB}.corrupt-${TS}.bak" 2>/dev/null
            rm -f "${DEDUP_DB}-wal" "${DEDUP_DB}-shm"
            rm -f "${HOME}/.surrogate/.dedup-bootstrap-done"
            echo "[$(date +%H:%M:%S)] WIPED corrupt dedup.db → ${DEDUP_DB}.corrupt-${TS}.bak (forcing re-bootstrap)" >> "$LOG_DIR/boot.log"
        fi
    fi

    # ── One-time central dedup bootstrap from existing data ──────────────
    if [[ ! -f "${HOME}/.surrogate/.dedup-bootstrap-done" ]]; then
        echo "[$(date +%H:%M:%S)] running central dedup bootstrap (one-time)" >> "$LOG_DIR/boot.log"
        nohup bash "${HOME}/.surrogate/bin/dedup-bootstrap.sh" > "$LOG_DIR/dedup-bootstrap.log" 2>&1 &
    fi

    # ── BOOT-TIME enrich kickoff (trigger immediate pull, don't wait for cron) ──
    # User feedback: 'ตั้งแต่ 7 โมงเช้า แต่ data ไม่เพิ่ม' — cron M%60 may have
    # been mis-aligned with rebuilds. Force one enrich run on every boot.
    nohup bash "${HOME}/.surrogate/bin/dataset-enrich.sh" >> "$LOG_DIR/dataset-enrich.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] boot-time dataset-enrich kicked off" >> "$LOG_DIR/boot.log"

    # ── BOOT-TIME kaggle-trainer kickoff (don't wait for 90-min cron) ─────
    # Submits the LoRA training notebook to Kaggle T4 immediately on every
    # Space boot. Kaggle CLI gracefully handles 'already running' state.
    nohup bash "${HOME}/.surrogate/bin/kaggle-trainer.sh" >> "$LOG_DIR/kaggle-trainer.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] boot-time kaggle-trainer kicked off" >> "$LOG_DIR/boot.log"

    # ── BOOT-TIME lightning-trainer kickoff — H200 4 hr free for big model ─
    nohup bash "${HOME}/.surrogate/bin/lightning-trainer.sh" >> "$LOG_DIR/lightning-trainer.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] boot-time lightning-trainer kicked off (H200 4hr quota)" >> "$LOG_DIR/boot.log"

    # ── BOOT-TIME dataset-mirror — bulk-clone top community SFT mixes ──────
    # Far faster than streaming-and-normalize — 1 commit per parquet file
    # = millions of pairs landing as raw mirrors/<slug>/<file>. Idempotent
    # via stamp file so we don't re-mirror what's already been pulled.
    nohup bash "${HOME}/.surrogate/bin/dataset-mirror.sh" >> "$LOG_DIR/dataset-mirror.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] boot-time dataset-mirror kicked off (30 community sources)" >> "$LOG_DIR/boot.log"

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
# redis cap tightened on LOW_MEM (was 1gb → 256mb). Coordinator uses
# SQLite directly; redis is only a soft cache for work-queue priorities.
REDIS_MAX="${REDIS_MAX:-$([[ "$LOW_MEM" == "1" ]] && echo "256mb" || echo "1gb")}"
redis-server --daemonize yes --port 6379 --bind 127.0.0.1 \
    --maxmemory "$REDIS_MAX" --maxmemory-policy allkeys-lru
sleep 1
redis-cli -h 127.0.0.1 -p 6379 ping >> "$LOG_DIR/redis.log" 2>&1

# ── 5. Ollama — DISABLED on cpu-basic (16 GB limit) ───────────────────────
# Root cause of 7-hr Runtime Error 2026-04-29: ollama loading qwen3-coder:30b
# (~17 GB Q4) + qwen2.5-coder:14b (~9 GB) + granite (~5 GB) = ~31 GB of model
# weights against a 16 GB cap → instant OOM on any inference.
#
# On cpu-basic the FREE LLM LADDER (cerebras/groq/openrouter/gemini/chutes)
# is faster anyway — wafer-scale inference beats CPU x86 by 50-200×.
# Ollama only worth running once Space upgrades to ≥cpu-upgrade (32 GB) OR
# moves to OCI A1.Flex anchor (24 GB ARM, native ollama support).
#
# Set LOW_MEM=0 to re-enable on bigger Space tier.
LOW_MEM="${LOW_MEM:-1}"
if [[ "$LOW_MEM" == "1" ]]; then
    echo "[$(date +%H:%M:%S)] ⚠ ollama SKIPPED (LOW_MEM=1, cpu-basic 16 GB)" \
        >> "$LOG_DIR/boot.log"
    echo "[$(date +%H:%M:%S)]   → free LLM ladder serves all v2 inference" \
        >> "$LOG_DIR/boot.log"
else
    OLLAMA_MODELS="${HOME}/.ollama/models" \
    OLLAMA_HOST=127.0.0.1:11434 \
    nohup ollama serve > "$LOG_DIR/ollama.log" 2>&1 &
    sleep 6
    (
        if ! ollama list 2>/dev/null | grep -q "nomic-embed-text"; then
            ollama pull nomic-embed-text > "$LOG_DIR/ollama-pull-embed.log" 2>&1
        fi
        if ! ollama list 2>/dev/null | grep -q "qwen2.5-coder:3b"; then
            # Smallest coder that's actually useful — fits any tier
            ollama pull qwen2.5-coder:3b > "$LOG_DIR/ollama-pull-3b.log" 2>&1
        fi
    ) &
fi

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

# ── 7a. Continuous scrape daemon — concurrency tuned to LOW_MEM ────────────
SCRAPE_PARALLEL="${SCRAPE_PARALLEL:-$([[ "$LOW_MEM" == "1" ]] && echo 2 || echo 8)}"
cat > /tmp/scrape-daemon.sh <<SCRAPESH
#!/bin/bash
set -a; source ~/.hermes/.env 2>/dev/null; set +a
LOG="\${HOME}/.surrogate/logs/scrape-continuous.log"
mkdir -p "\$(dirname "\$LOG")"
while true; do
    START=\$(date +%s)
    bash ~/.surrogate/bin/domain-scrape-loop.sh 1500 ${SCRAPE_PARALLEL} >> "\$LOG" 2>&1
    DUR=\$(( \$(date +%s) - START ))
    if [[ \$DUR -lt 30 ]]; then sleep 30
    elif [[ \$DUR -lt 120 ]]; then sleep 15
    else sleep 5
    fi
done
SCRAPESH
chmod +x /tmp/scrape-daemon.sh
nohup /tmp/scrape-daemon.sh > "$LOG_DIR/scrape-daemon.log" 2>&1 &
echo "[$(date +%H:%M:%S)] scrape daemon parallel=${SCRAPE_PARALLEL} (LOW_MEM=$LOW_MEM)" >> "$LOG_DIR/boot.log"

# ── 7b. Agentic crawler — DISABLED on LOW_MEM (anchor takes this load) ─────
if [[ "$LOW_MEM" != "1" ]]; then
    CRAWLER_PARALLEL="${CRAWLER_PARALLEL:-6}"
    nohup bash ~/.surrogate/bin/agentic-crawler.sh "$CRAWLER_PARALLEL" \
        > "$LOG_DIR/agentic-crawler.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] agentic crawler parallel=$CRAWLER_PARALLEL" >> "$LOG_DIR/boot.log"
else
    echo "[$(date +%H:%M:%S)] ⚠ agentic-crawler SKIPPED (LOW_MEM); anchor handles" >> "$LOG_DIR/boot.log"
fi

# ── 7b2. GitHub-specific agentic crawler (lightweight, keep on always) ─────
nohup bash ~/.surrogate/bin/github-agentic-crawler.sh > "$LOG_DIR/github-agentic-crawler.log" 2>&1 &
echo "[$(date +%H:%M:%S)] github-agentic-crawler started" >> "$LOG_DIR/boot.log"

# ── 7b3. HF Dataset Discoverer — DISABLED (replaced by continuous-discoverer) ─
# Round 10 (a27499d): bin/v2/continuous-discoverer.sh covers HF + arxiv +
# Stack Exchange + GH trending in one daemon. Old hf-dataset-discoverer.sh
# is now redundant + memory pressure on cpu-basic.
echo "[$(date +%H:%M:%S)] ⚠ hf-dataset-discoverer SKIPPED (replaced by continuous-discoverer)" >> "$LOG_DIR/boot.log"

# ── 7e. auto-orchestrate-continuous — DISABLED on LOW_MEM (cron handles it) ─
if [[ "$LOW_MEM" != "1" ]]; then
    nohup bash ~/.surrogate/bin/auto-orchestrate-continuous.sh > "$LOG_DIR/auto-orchestrate-continuous.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] auto-orchestrate-continuous started (4 parallel workers)" >> "$LOG_DIR/boot.log"
else
    echo "[$(date +%H:%M:%S)] ⚠ auto-orchestrate-continuous SKIPPED (LOW_MEM); cron slot at M%20==0 covers it" >> "$LOG_DIR/boot.log"
fi

# ── 7e1. SELF-HEAL WATCHDOG — must start BEFORE memory-hungry workers ───────
# Monitors RAM usage every 60s; preempts youngest dataset-enrich shard if
# usage >= 85% to dodge the cpu-basic 16Gi OOM kill that would otherwise
# crash the entire container. Also restarts stuck ingest / kicks stale uploader.
nohup bash ~/.surrogate/bin/self-heal-watchdog.sh > "$LOG_DIR/self-heal-watchdog.log" 2>&1 &
echo "[$(date +%H:%M:%S)] self-heal-watchdog started (mem<85%, ingest<20m, push<10m)" >> "$LOG_DIR/boot.log"

# ── 7e2. GH-ACTIONS TICKER — burst-dispatch external runners every 60s ──────
# Fires workflow_dispatch on arkashira/ashiradevops-alt runner repos every
# 60s, bypassing GitHub's */5 cron minimum. Combined with 8-min runner
# timeouts, the 20-concurrent free-tier slot cap stays saturated.
# Skips silently if GH_TOKEN_ARKASHIRA / GH_TOKEN_DEVOPS aren't set as
# Space secrets — operator can add later without restart-required.
nohup bash ~/.surrogate/bin/gh-actions-ticker.sh > "$LOG_DIR/gh-actions-ticker.log" 2>&1 &
echo "[$(date +%H:%M:%S)] gh-actions-ticker started (60s tick, dispatches arkashira+ashiradevops-alt)" >> "$LOG_DIR/boot.log"

# ── 7e3. LLM BURST GENERATOR — synthetic training pairs from 8 free LLMs ────
# Cerebras + Groq + OpenRouter + Gemini + Chutes + NV NIM + Samba + Kimi.
# Each cycle fires 3 prompts at every active provider in parallel, writes
# {prompt, response} pairs to training-pairs.jsonl. Combined free-tier
# budget: ~7000+ pairs/day. Skips any provider whose key env is not set.
if [[ "$LOW_MEM" != "1" ]]; then
    nohup python3 ~/.surrogate/bin/llm-burst-generator.py > "$LOG_DIR/llm-burst-generator.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] llm-burst-generator started (8 LLM APIs in parallel, ~7K synthetic pairs/day)" >> "$LOG_DIR/boot.log"
else
    echo "[$(date +%H:%M:%S)] ⚠ llm-burst-generator SKIPPED (LOW_MEM); ZeroGPU synth-puller covers" >> "$LOG_DIR/boot.log"
fi
sleep 3   # Stagger spawns — avoid memory burst at boot

# ── 7f. PARALLEL BULK INGEST (slug-hash sharded; 6 shards on cpu-basic) ─────
# Was 16 shards but caused 'Memory limit exceeded (16Gi)' OOM. Each shard
# peaks ~1 GB while streaming via 'datasets' lib. Watchdog above provides
# a second safety net if peak still spikes.
if [[ "$LOW_MEM" != "1" ]]; then
    nohup bash ~/.surrogate/bin/bulk-ingest-parallel.sh > "$LOG_DIR/bulk-ingest-parallel.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] bulk-ingest-parallel started (6 shards, 293M total cap)" >> "$LOG_DIR/boot.log"
else
    echo "[$(date +%H:%M:%S)] ⚠ bulk-ingest-parallel SKIPPED (LOW_MEM); streaming-mirror-worker covers" >> "$LOG_DIR/boot.log"
fi
sleep 3

# ── 7g. PARQUET-DIRECT INGEST (skip 'datasets' library overhead, 5-10× faster) ──
# Downloads parquet shards directly via HF datasets-server API + pyarrow filter.
# Targets only trillion-scale corpora where streaming is too slow.
# DLs reduced to 2 parallel — combined with 6 ingest shards stays under 16Gi.
PARQUET_PARALLEL=2 nohup bash ~/.surrogate/bin/parquet-direct-ingest.sh > "$LOG_DIR/parquet-direct-ingest.log" 2>&1 &
echo "[$(date +%H:%M:%S)] parquet-direct-ingest started (2 parallel DLs)" >> "$LOG_DIR/boot.log"

# ── 7c. Skill-synthesis daemon — DISABLED on LOW_MEM (heavy LLM calls) ────
if [[ "$LOW_MEM" != "1" ]]; then
    nohup bash ~/.surrogate/bin/skill-synthesis-daemon.sh > "$LOG_DIR/skill-synthesis.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] skill-synthesis daemon started" >> "$LOG_DIR/boot.log"
else
    echo "[$(date +%H:%M:%S)] ⚠ skill-synthesis SKIPPED (LOW_MEM); anchor's voyager-skills.py covers" >> "$LOG_DIR/boot.log"
fi

# ── 7d. Bulk mirror coordinator + 4 parallel workers ────────────────────────
# User feedback 2026-04-29: "ทุก agent ทำงานร่วมกัน และไม่ไปที่ซ้ำๆ".
# Coordinator = SQLite claim queue (~/.surrogate/state/bulk-mirror-claims.db).
# Workers each pull next pending dataset, mirror+sanitize+dedup, mark done.
# 100+ massive datasets in bin/v2/bulk-datasets-massive.txt (code/security/SDLC/agent/etc).
# Lease-based claims (15 min) — crashes auto-expire so other workers pick up.
python3 ~/.surrogate/bin/v2/bulk-mirror-coordinator.py seed >> "$LOG_DIR/bulk-mirror-seed.log" 2>&1 || true

# Two worker types share the same coordinator queue:
#   bulk-mirror-worker.sh    — full-download, suits small/medium datasets
#   streaming-mirror-worker.sh — HF datasets streaming, suits trillion-token
# LOW_MEM tuned for cpu-basic 16GB after Round 9+10 OOM:
# 0 bulk (full-download too heavy) + 2 streaming (lighter) on LOW_MEM
# Anchor handles bulk via 24GB ARM headroom.
# Tighter further after Round 11+12 OOM:
# 1 streaming worker on LOW_MEM (was 2). Anchor takes the slack via 6 workers.
BULK_WORKERS="${BULK_WORKERS:-$([[ "$LOW_MEM" == "1" ]] && echo 0 || echo 4)}"
STREAM_WORKERS="${STREAM_WORKERS:-$([[ "$LOW_MEM" == "1" ]] && echo 1 || echo 4)}"

for i in $(seq 1 "$BULK_WORKERS"); do
    nohup bash ~/.surrogate/bin/v2/bulk-mirror-worker.sh "bulk-w$i" \
        > "$LOG_DIR/bulk-worker-$i.log" 2>&1 &
done
for i in $(seq 1 "$STREAM_WORKERS"); do
    nohup bash ~/.surrogate/bin/v2/streaming-mirror-worker.sh "stream-w$i" \
        > "$LOG_DIR/stream-worker-$i.log" 2>&1 &
done
TOTAL_WORKERS=$((BULK_WORKERS + STREAM_WORKERS))
echo "[$(date +%H:%M:%S)] bulk-mirror coordinator + $BULK_WORKERS bulk + $STREAM_WORKERS streaming = $TOTAL_WORKERS workers (200+ datasets queued, LOW_MEM=$LOW_MEM)" >> "$LOG_DIR/boot.log"

# ── 7d2. Continuous multi-source dataset discoverer (boot daemon, never exits) ─
# Replaces aggressive-harvester cron — runs always, sweeps HF + arxiv + SE + GH.
if ! pgrep -f "continuous-discoverer.sh" >/dev/null; then
    nohup bash ~/.surrogate/bin/v2/continuous-discoverer.sh \
        > "$LOG_DIR/continuous-discoverer.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] continuous-discoverer started (HF + arxiv + SE + GH, ~5min cycle)" >> "$LOG_DIR/boot.log"
fi

# ── Auto-startup-loop: 45 personae × 9 LoRA clusters × auto-commit + auto-push ─
# CEO/CTO/CMO/CFO/COO/PM/UX/Designer/SRE/DevOps/Marketing/SDR/AE/Growth/CS/Legal/HR/etc.
# 1 role per 15-min cycle; chained roles fire downstream automatically.
if ! pgrep -f "auto-startup-loop.sh" >/dev/null; then
    nohup bash ~/.surrogate/bin/v2/auto-startup-loop.sh \
        > "$LOG_DIR/auto-startup-loop.log" 2>&1 &
    echo "[$(date +%H:%M:%S)] auto-startup-loop started (45 personae cycle 15min, chains, auto-commit)" >> "$LOG_DIR/boot.log"
fi

# ── 7d. Train-ready pusher — disabled at boot for now. Caused Space
#       RUNTIME_ERROR on first deployment (2026-04-29). Script kept at
#       bin/train-ready-pusher.sh; launch manually after Space proves stable:
#         nohup bash ~/.surrogate/bin/train-ready-pusher.sh > /tmp/trp.log 2>&1 &
# nohup bash ~/.surrogate/bin/train-ready-pusher.sh > "$LOG_DIR/train-ready-pusher.log" 2>&1 &

# ── 7b. Cron loop — non-scrape daemons (scrape now runs continuously above) ─
cat > /tmp/hermes-cron.sh <<'CRONSH'
#!/bin/bash
set -a; source ~/.hermes/.env 2>/dev/null; set +a
LOG="${HOME}/.surrogate/logs/cron.log"
mkdir -p "$(dirname "$LOG")"
while true; do
    M=$(($(date +%s) / 60))
    # Cron offsets STAGGERED — minute=0 burst was OOM trigger.
    # Each major task picks a unique M%X==N offset so no two fire together.
    [[ $((M % 2)) -eq 1 ]] && bash ~/.surrogate/bin/surrogate-dev-loop.sh 1 >> "$LOG" 2>&1 &
    [[ $((M % 5)) -eq 2 ]] && bash ~/.surrogate/bin/work-queue-producer.sh >> "$LOG" 2>&1 &
    # Auto-scaler — spawn/kill workers based on free memory tier (burst-but-don't-die)
    [[ $((M % 5)) -eq 4 ]] && bash ~/.surrogate/bin/v2/auto-scaler.sh >> "$LOG" 2>&1 &
    # synth-puller — hit surrogate1 ZeroGPU /api/synth_batch every 5 min
    # Drains free PRO 25K min/mo into Magpie-style training pairs (16 domains rotate).
    [[ $((M % 5)) -eq 3 ]] && bash ~/.surrogate/bin/v2/synth-puller.sh >> "$LOG" 2>&1 &
    # push-training-to-hf gated by memory (loads big shard into RAM).
    # Anchor (24GB) takes over when capacity arrives — see anchor cron-loop.
    [[ $((M % 3)) -eq 1 ]] && bash ~/.surrogate/bin/v2/memory-guard.sh \
        && bash ~/.surrogate/bin/push-training-to-hf.sh >> "$LOG" 2>&1 &
    # auto-orchestrate now runs CONTINUOUSLY (4 parallel workers) — see step 7e below.
    # Cron entry retained for legacy single-fire boost (no harm if continuous already up):
    [[ $((M % 20)) -eq 0 ]] && pgrep -f "auto-orchestrate-continuous" >/dev/null || bash ~/.surrogate/bin/auto-orchestrate-loop.sh >> "$LOG" 2>&1 &
    # Every 30 min: research-apply (pop queue → orchestrate → ship feature)
    [[ $((M % 30)) -eq 15 ]] && bash ~/.surrogate/bin/surrogate-research-apply.sh >> "$LOG" 2>&1 &
    # Every 60 min: keyword tuner (adapts scrape queue based on yields)
    [[ $((M % 60)) -eq 4 ]] && bash ~/.surrogate/bin/scrape-keyword-tuner.sh >> "$LOG" 2>&1 &
    # Every 6 hours: research-loop (discover new features from competitors/papers)
    [[ $((M % 360)) -eq 30 ]] && bash ~/.surrogate/bin/surrogate-research-loop.sh >> "$LOG" 2>&1 &
    # Every 60 min: dataset enrich. Memory-guarded — full HF Hub iter is heavy.
    [[ $((M % 60)) -eq 5 ]] && bash ~/.surrogate/bin/v2/memory-guard.sh \
        && bash ~/.surrogate/bin/dataset-enrich.sh >> "$LOG" 2>&1 &
    # Every 15 min: self-ingest training-pairs into FTS index (closes self-improvement)
    [[ $((M % 15)) -eq 3 ]] && bash ~/.surrogate/bin/surrogate-self-ingest.sh >> "$LOG" 2>&1 &
    # Every 30 min: build vector embeddings index (RAG semantic search)
    [[ $((M % 30)) -eq 12 ]] && bash ~/.surrogate/bin/rag-vector-builder.sh >> "$LOG" 2>&1 &
    # Every 30 min: synthetic data generation (REWORK→APPROVE DPO + distilabel rewrite)
    [[ $((M % 30)) -eq 7 ]] && bash ~/.surrogate/bin/v2/memory-guard.sh \
        && bash ~/.surrogate/bin/synthetic-data-from-rework.sh >> "$LOG" 2>&1 &
    # Daily 04:00 UTC: refresh CVE feed (NVD + CISA KEV) → security-knowledge dataset
    [[ $((M % 1440)) -eq 240 ]] && bash ~/.surrogate/bin/refresh-cve-feed.sh >> "$LOG" 2>&1 &
    # Daily 05:00 UTC: scrape SRE postmortems (danluu list + awesome-tech-postmortems)
    [[ $((M % 1440)) -eq 300 ]] && bash ~/.surrogate/bin/scrape-sre-postmortems.sh >> "$LOG" 2>&1 &
    # Daily 06:00 UTC: LLM-expand role keywords (sends each role's skills to
    # Cerebras/Groq → +80 specific job-description-style search terms each).
    # Discoverer auto-uses the expanded list on its next cycle.
    [[ $((M % 1440)) -eq 360 ]] && python3 ~/.surrogate/bin/expand-role-keywords.py >> "$LOG_DIR/expand-role-keywords.log" 2>&1 &
    # Every 90 min: kick a Kaggle T4 LoRA training run on the latest dataset
    # slice. Free Kaggle quota = 30 hr/week per account; one full run = 4-6 hr,
    # so we DO want to keep submitting — Kaggle queues if 1 already running,
    # auto-cancels older if 5+ pending. With shorter interval we keep the
    # GPU pipeline saturated.
    [[ $((M % 90)) -eq 5 ]] && bash ~/.surrogate/bin/kaggle-trainer.sh >> "$LOG_DIR/kaggle-trainer.log" 2>&1 &
    # Every 6 hr: Lightning AI H200 training run (free 4hr H200 quota = ~13/mo).
    # H200 141GB VRAM fits Qwen3-Coder-480B-A35B QLoRA — biggest free training.
    [[ $((M % 360)) -eq 45 ]] && bash ~/.surrogate/bin/lightning-trainer.sh >> "$LOG_DIR/lightning-trainer.log" 2>&1 &

    # ── Round 5 (2026-04) sustainability loops ──────────────────────────
    # Every 6 hr (offset 90): self-improve loop — gen problems, judge,
    # winners → training data, losers → reflexion-store.
    [[ $((M % 360)) -eq 90 ]] && bash ~/.surrogate/bin/v2/memory-guard.sh \
        && bash ~/.surrogate/bin/v2/self-improve-loop.sh >> "$LOG_DIR/self-improve.log" 2>&1 &
    # Every 30 min (offset 22): mine new tool-call traces from logs into
    # SFT + DPO data, plus voyager skill candidates.
    [[ $((M % 30)) -eq 22 ]] && python3 ~/.surrogate/bin/v2/tool-trace-collector.py >> "$LOG_DIR/tool-trace.log" 2>&1 &
    # Every 60 min (offset 17): export promoted voyager skills to JSONL
    # (training-data slice + inference-time retrieval source).
    [[ $((M % 60)) -eq 17 ]] && python3 ~/.surrogate/bin/v2/voyager-skills.py export >> "$LOG_DIR/voyager.log" 2>&1 &
    # Daily 07:00 UTC: active-learning batch from one bulk-mirror file.
    # Skips silently if no pool yet.
    [[ $((M % 1440)) -eq 420 ]] && {
        POOL=$(ls -t "$DATA"/bulk-mirror/*.jsonl 2>/dev/null | head -1)
        [[ -n "$POOL" ]] && python3 ~/.surrogate/bin/v2/active-learning.py \
            --pool "$POOL" --n 200 --scan 1500 \
            >> "$LOG_DIR/active-learning.log" 2>&1 &
    }
    # Daily 08:00 UTC: constitutional self-critique on yesterday's
    # winners (pulls latest self-improve winners file).
    [[ $((M % 1440)) -eq 480 ]] && {
        WIN=$(ls -t "$DATA"/v2/self-improve/winners-*.jsonl 2>/dev/null | head -1)
        [[ -n "$WIN" ]] && python3 ~/.surrogate/bin/v2/constitutional-loop.py \
            --input "$WIN" --n 200 \
            >> "$LOG_DIR/constitutional.log" 2>&1 &
    }

    # ── Round 7+8 (2026-04-30) — trillion-scale + harvester + enrich ──────
    # Every 30 min (offset 9): aggressive HF dataset discoverer (70-keyword sweep)
    [[ $((M % 30)) -eq 9 ]] && bash ~/.surrogate/bin/v2/aggressive-harvester.sh \
        >> "$LOG_DIR/aggressive-harvester.log" 2>&1 &
    # Every 60 min (offset 35): enrich newly-mirrored bulk files
    [[ $((M % 60)) -eq 35 ]] && bash ~/.surrogate/bin/v2/memory-guard.sh \
        && bash ~/.surrogate/bin/v2/enrich-pipeline.sh \
        >> "$LOG_DIR/enrich-pipeline.log" 2>&1 &
    # Every 30 min (offset 25): spawn extra streaming worker if pool empty
    [[ $((M % 30)) -eq 25 ]] && {
        if ! pgrep -f "streaming-mirror-worker.sh" >/dev/null; then
            nohup bash ~/.surrogate/bin/v2/streaming-mirror-worker.sh "stream-cron-$(date +%s)" \
                > "$LOG_DIR/stream-worker-cron.log" 2>&1 &
        fi
    }
    # Daily 09:00 UTC: teachable-prompt filter on harvested data
    [[ $((M % 1440)) -eq 540 ]] && {
        LATEST=$(ls -t "$DATA"/v2/enriched/*.jsonl 2>/dev/null | head -1)
        [[ -n "$LATEST" ]] && python3 ~/.surrogate/bin/v2/teachable-prompt-filter.py \
            --input "$LATEST" --out "$DATA"/v2/teachable-$(date +%Y%m%d).jsonl \
            --n 1000 --keep-target 200 \
            >> "$LOG_DIR/teachable.log" 2>&1 &
    }
    # Daily 11:00 UTC: regression test suite (catches breakage post-push)
    [[ $((M % 1440)) -eq 660 ]] && bash ~/.surrogate/bin/v2/regression-test.sh --quick \
        >> "$LOG_DIR/regression.log" 2>&1 &
    # Weekly Sun 10:00 UTC: abstract-cot compress reasoning data
    [[ $((M % 10080)) -eq 600 ]] && {
        for f in "$DATA"/v2/verify-traces.jsonl "$DATA"/v2/self-improve/winners-*.jsonl; do
            [[ -f "$f" ]] || continue
            python3 ~/.surrogate/bin/v2/abstract-cot-compressor.py \
                --input "$f" --out "${f%.jsonl}-compressed.jsonl" \
                >> "$LOG_DIR/abstract-cot.log" 2>&1
        done
    }
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
