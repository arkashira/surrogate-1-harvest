#!/usr/bin/env bash
# post-create.sh — runs ONCE at codespace creation. Idempotent so re-running
# during local debugging is safe.
#
# Responsibilities:
#   1. Install Python + Node deps for the harvest repo
#   2. Install ollama (LLM runtime) and pull the Qwen2.5-Coder-7B 4-bit model
#   3. Drop a systemd-user unit for ollama so it survives shell exit and
#      restarts on container start (postStartCommand re-enables it)
#
# Why a script instead of inline JSON: a 200-char one-liner is unreadable,
# and detached background processes (`nohup ... &`) get reaped when the
# devcontainer's spawning shell exits. systemd-user owns the lifecycle.
set -euo pipefail
cd /workspaces/surrogate-1-harvest

log() { echo "[post-create $(date -u +%H:%M:%SZ)] $*"; }

log "1/4 python deps"
pip install --no-cache-dir -r requirements.txt
# Note: osv-scanner is a Go binary, not a PyPI package — skip in containers
# without Go. Re-enable later via `go install github.com/google/osv-scanner/...@v1`
# inside a feature that provisions Go.

log "2/4 node deps"
npm install -g wrangler@4 @commitlint/cli@19 @commitlint/config-conventional@19 @usebruno/cli

log "3/4 install ollama"
if ! command -v ollama >/dev/null 2>&1; then
    # ollama installer (≥0.5.x) ships its tarball as .tar.zst — needs zstd
    sudo apt-get update -qq && sudo apt-get install -y -qq zstd
    curl -fsSL https://ollama.com/install.sh | sh
fi

log "4/4 launch ollama + pull model"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/ollama.service" <<'UNIT'
[Unit]
Description=ollama LLM proxy (codespace)
After=default.target

[Service]
Environment=OLLAMA_HOST=0.0.0.0:11434
Environment=OLLAMA_KEEP_ALIVE=24h
ExecStart=/usr/local/bin/ollama serve
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT

# Codespaces: systemd-user generally not enabled. Fall back to setsid+nohup
# which DOES detach from the spawning shell properly (unlike plain `&`).
if loginctl enable-linger "$(id -un)" 2>/dev/null && \
   systemctl --user daemon-reload 2>/dev/null && \
   systemctl --user enable --now ollama.service 2>/dev/null; then
    log "ollama under systemd-user"
else
    log "systemd-user unavailable, using setsid fallback"
    pgrep -x ollama >/dev/null 2>&1 || \
        setsid bash -c 'OLLAMA_HOST=0.0.0.0:11434 OLLAMA_KEEP_ALIVE=24h ollama serve > /tmp/ollama.log 2>&1' &
    disown 2>/dev/null || true
fi

# Wait for ollama to listen, then pull the model
for i in $(seq 1 30); do
    curl -sf -m 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 2
done

log "pulling qwen2.5-coder:7b-instruct-q4_K_M (~4.7 GB, may take 5-10 min)"
ollama pull qwen2.5-coder:7b-instruct-q4_K_M
log "done"
