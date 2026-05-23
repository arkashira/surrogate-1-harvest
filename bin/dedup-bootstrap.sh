#!/usr/bin/env bash
# Bootstrap central dedup store from existing data.
# Run ONCE on first boot (idempotent — safe to re-run).
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/dedup-bootstrap.log"
mkdir -p "$(dirname "$LOG")"
DEDUP_DB="$HOME/.surrogate/state/dedup.db"

# Single-instance guard — prevents two start.sh runs (e.g., container restarts)
# from kicking off concurrent bootstraps that fight over dedup.db locks.
LOCK="$HOME/.surrogate/state/dedup-bootstrap.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
    echo "[$(date +%H:%M:%S)] another bootstrap already running — exiting" | tee -a "$LOG"
    exit 0
fi

echo "[$(date +%H:%M:%S)] dedup bootstrap start (locked)" | tee -a "$LOG"

# 1. Bootstrap from local training-pairs.jsonl
if [[ -f "$HOME/.surrogate/training-pairs.jsonl" ]]; then
    echo "  ingesting local training-pairs.jsonl..." | tee -a "$LOG"
    cat "$HOME/.surrogate/training-pairs.jsonl" | python3 "$HOME/.surrogate/bin/lib/dedup.py" bootstrap "local-jsonl" 2>&1 | tee -a "$LOG"
fi

# 2. Bootstrap from HF dataset existing files (download metadata-only sample)
# Skip the 3.8GB auto-orchestrate file (too big to fetch on free tier)
SMALL_FILES=(
    "2026-04-21.jsonl"
    "2026-04-22.jsonl"
    "claude-2026-04-27.jsonl"
    "claude-2026-04-28.jsonl"
    "dpo-pairs.jsonl"
    "github-domain-2026-04-27.jsonl"
    "github-public-2026-04-24.jsonl"
    "local-dev-pending.jsonl"
)

HF_AUTH="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"
if [[ -n "$HF_AUTH" ]]; then
    for f in "${SMALL_FILES[@]}"; do
        url="https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/$f"
        echo "  fetching $f..." | tee -a "$LOG"
        curl -sS --max-time 120 -H "Authorization: Bearer $HF_AUTH" "$url" 2>/dev/null \
            | python3 "$HOME/.surrogate/bin/lib/dedup.py" bootstrap "hf-$f" 2>&1 | tee -a "$LOG"
    done
fi

# 3. Print final stats
python3 "$HOME/.surrogate/bin/lib/dedup.py" stats 2>&1 | tee -a "$LOG"

# Marker so we don't re-bootstrap
touch "$HOME/.surrogate/.dedup-bootstrap-done"
echo "[$(date +%H:%M:%S)] bootstrap done" | tee -a "$LOG"
