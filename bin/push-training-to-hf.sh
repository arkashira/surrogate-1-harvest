#!/usr/bin/env bash
# Push training pairs → HF dataset, INCREMENTALLY in small batches.
#
# Strategy: never upload the whole file. Each cron run pushes ONE chunk of
# CHUNK_SIZE pairs to a date-stamped file (one per day). Small uploads = fast,
# resilient, avoid timeouts on large blobs.
#
# Idempotent: tracks last-pushed line offset. Only advances on success.
# If 35K pairs queued, drains over ~17 min (CHUNK_SIZE=1500 every 3 min).
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

SRC="$HOME/.surrogate/training-pairs.jsonl"
OFFSET_FILE="$HOME/.surrogate/.training-push-offset"
LOG="$HOME/.surrogate/logs/training-push.log"
CHUNK_SIZE="${TRAINING_PUSH_CHUNK:-1500}"
mkdir -p "$(dirname "$LOG")"

[[ ! -f "$SRC" ]] && { echo "[$(date +%H:%M:%S)] no source $SRC" | tee -a "$LOG"; exit 0; }

CUR_LINES=$(wc -l < "$SRC" | tr -d ' ')
PREV_OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
QUEUED=$(( CUR_LINES - PREV_OFFSET ))

echo "[$(date +%H:%M:%S)] queued=$QUEUED (offset=$PREV_OFFSET total=$CUR_LINES chunk=$CHUNK_SIZE)" | tee -a "$LOG"
[[ $QUEUED -le 0 ]] && exit 0

# Take just one chunk (don't try to push everything at once — that's why it kept failing)
TAKE=$QUEUED
[[ $TAKE -gt $CHUNK_SIZE ]] && TAKE=$CHUNK_SIZE

# Resolve token
HF_AUTH="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-${HUGGINGFACE_TOKEN:-}}}"
if [[ -z "$HF_AUTH" ]]; then
    echo "[$(date +%H:%M:%S)] ERR: no HF_TOKEN — cannot upload" | tee -a "$LOG"
    exit 1
fi

# Slice this chunk to a unique-per-cron-fire file (no overwrite)
DATE_TAG=$(date +%Y-%m-%d)
TIME_TAG=$(date +%H%M%S)
SLICE_DIR="$HOME/.surrogate/.push-slices"
mkdir -p "$SLICE_DIR"
SLICE="$SLICE_DIR/${DATE_TAG}_${TIME_TAG}.jsonl"

# Take TAKE lines starting AFTER prev offset
sed -n "$((PREV_OFFSET + 1)),$((PREV_OFFSET + TAKE))p" "$SRC" > "$SLICE"
SLICE_LINES=$(wc -l < "$SLICE" | tr -d ' ')
SLICE_BYTES=$(wc -c < "$SLICE" | tr -d ' ')
echo "[$(date +%H:%M:%S)] uploading slice: $SLICE_LINES lines / $((SLICE_BYTES/1024)) KB" | tee -a "$LOG"

# Upload to a chunk-specific filename — never overwrites, just adds new files
NEW_OFFSET=$(( PREV_OFFSET + TAKE ))
REMOTE_PATH="batches/${DATE_TAG}/chunk-${TIME_TAG}-${NEW_OFFSET}.jsonl"

if HF_AUTH="$HF_AUTH" python3 - "$SLICE" "$REMOTE_PATH" "$SLICE_LINES" >> "$LOG" 2>&1 <<'PYEOF'
import sys, os, time
slice_path, remote, n_lines = sys.argv[1], sys.argv[2], sys.argv[3]
hf_auth = os.environ["HF_AUTH"]

try:
    from huggingface_hub import HfApi
    from huggingface_hub.errors import HfHubHTTPError
except ImportError:
    print(f"[{time.strftime('%H:%M:%S')}] ERR: huggingface_hub not installed")
    sys.exit(2)

api = HfApi(token=hf_auth)

# 5-attempt retry with exponential backoff. The repo has a hard 128
# commits/hr ceiling that gets hit when 40+ GH Actions runners + Space
# shards all push at once. Backoff lets that ceiling drift back down.
delays = [30, 90, 240, 600, 1200]   # 30s, 1.5m, 4m, 10m, 20m  (~36 min total worst-case)
for attempt, delay in enumerate([0] + delays):
    if delay:
        time.sleep(delay)
    try:
        api.upload_file(
            path_or_fileobj=slice_path,
            path_in_repo=remote,
            repo_id="axentx/surrogate-1-training-pairs",
            repo_type="dataset",
            commit_message=f"chunk: +{n_lines} pairs ({time.strftime('%H:%M')})",
        )
        print(f"[{time.strftime('%H:%M:%S')}] ✅ uploaded → {remote} (attempt {attempt + 1})")
        sys.exit(0)
    except HfHubHTTPError as e:
        msg = str(e)
        if "429" in msg or "rate limit" in msg.lower() or "Too Many Requests" in msg:
            print(f"[{time.strftime('%H:%M:%S')}] ⚠ 429 on attempt {attempt + 1}/{len(delays)+1} — backing off {delays[attempt] if attempt < len(delays) else 0}s")
            continue
        # Non-429 hub errors: retry once with short delay then fail
        print(f"[{time.strftime('%H:%M:%S')}] ❌ {type(e).__name__}: {msg[:200]}")
        if attempt < 2: continue
        sys.exit(3)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] ❌ {type(e).__name__}: {str(e)[:200]}")
        if attempt < 2: continue
        sys.exit(3)
print(f"[{time.strftime('%H:%M:%S')}] ❌ all retries exhausted — slice will be retried next cron tick")
sys.exit(3)
PYEOF
then
    echo "$NEW_OFFSET" > "$OFFSET_FILE"
    rm -f "$SLICE"
    REMAINING=$(( CUR_LINES - NEW_OFFSET ))
    echo "[$(date +%H:%M:%S)] offset → $NEW_OFFSET · remaining=$REMAINING (next run)" | tee -a "$LOG"
else
    echo "[$(date +%H:%M:%S)] push failed — offset still $PREV_OFFSET, slice retained: $SLICE" | tee -a "$LOG"
    exit 1
fi
