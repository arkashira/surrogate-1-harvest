#!/usr/bin/env bash
# post-start.sh — runs every time the codespace starts (incl. wake from idle).
# Just needs to ensure ollama is running. Model is already cached locally
# from post-create, so this is fast (<5s once ollama listens).
set -euo pipefail

log() { echo "[post-start $(date -u +%H:%M:%SZ)] $*"; }

if pgrep -x ollama >/dev/null 2>&1; then
    log "ollama already running"
    exit 0
fi

if systemctl --user start ollama.service 2>/dev/null; then
    log "ollama started via systemd-user"
else
    log "systemd-user start failed, falling back to setsid"
    setsid bash -c 'OLLAMA_HOST=0.0.0.0:11434 OLLAMA_KEEP_ALIVE=24h ollama serve > /tmp/ollama.log 2>&1' &
    disown 2>/dev/null || true
fi

# Quick readiness gate so VS Code's port forwarder finds something listening
for i in $(seq 1 15); do
    curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && { log "ready"; exit 0; }
    sleep 1
done
log "WARN: ollama did not respond within 15s — check /tmp/ollama.log"
