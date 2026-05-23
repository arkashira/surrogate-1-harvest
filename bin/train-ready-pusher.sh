#!/usr/bin/env bash
# Train-ready pusher — every 5 min, push /data/training-pairs.jsonl content
# (latest N samples, gzip'd) to a FIXED path:
#   axentx/surrogate-1-training-pairs/train-ready/latest.jsonl.gz
#
# Why fixed path: Lightning training script can curl this URL directly via CDN
# without ANY HF API calls (no list_repo_files, no rate limit). Solves the
# 1000-req/5min token contention between HF Space daemons + training jobs.
#
# Format: each line = {"prompt": "...", "response": "..."} — same as live file.

set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/train-ready-pusher.log"
mkdir -p "$(dirname "$LOG")"
SOURCE="${HOME}/.surrogate/training-pairs.jsonl"
TARGET_REPO="axentx/surrogate-1-training-pairs"
TARGET_PATH="train-ready/latest.jsonl.gz"
MAX_LINES="${MAX_LINES:-200000}"  # 200K samples — Lightning can sample down

if [[ -z "${HF_TOKEN:-}" ]]; then
    echo "[$(date +%H:%M:%S)] train-ready-pusher: HF_TOKEN not set" | tee -a "$LOG"
    exit 0
fi

while true; do
    if [[ ! -f "$SOURCE" ]]; then
        echo "[$(date +%H:%M:%S)] source not found: $SOURCE" | tee -a "$LOG"
        sleep 300
        continue
    fi

    # Take latest N lines (most-recent samples = most-curated by self-improvement loop)
    TMP="/tmp/train-ready-$(date +%s).jsonl.gz"
    tail -n "$MAX_LINES" "$SOURCE" | gzip -c > "$TMP" 2>>"$LOG"
    BYTES=$(stat -c %s "$TMP" 2>/dev/null || echo 0)

    if [[ "$BYTES" -lt 1000 ]]; then
        echo "[$(date +%H:%M:%S)] file too small ($BYTES B), skip" | tee -a "$LOG"
        rm -f "$TMP"
        sleep 300
        continue
    fi

    echo "[$(date +%H:%M:%S)] pushing ${BYTES} bytes (${MAX_LINES} lines) → ${TARGET_PATH}" | tee -a "$LOG"

    HF_TOKEN="$HF_TOKEN" python3 - "$TMP" "$TARGET_REPO" "$TARGET_PATH" <<'PYEOF' 2>>"$LOG"
import sys, os, time
local_path, repo, remote = sys.argv[1], sys.argv[2], sys.argv[3]
from huggingface_hub import HfApi
from huggingface_hub.errors import HfHubHTTPError

api = HfApi(token=os.environ["HF_TOKEN"])
for attempt in range(5):
    try:
        api.upload_file(
            path_or_fileobj=local_path,
            path_in_repo=remote,
            repo_id=repo,
            repo_type="dataset",
            commit_message=f"train-ready pusher: latest snapshot {time.strftime('%H:%M')}",
        )
        print(f"  ✅ pushed → {repo}/{remote}")
        sys.exit(0)
    except HfHubHTTPError as e:
        if "429" in str(e):
            wait = 60 * (attempt + 1)
            print(f"  rate-limit; wait {wait}s")
            time.sleep(wait)
        else:
            print(f"  ❌ {type(e).__name__}: {str(e)[:200]}")
            sys.exit(1)
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)
print("  ❌ all retries exhausted")
sys.exit(1)
PYEOF

    rm -f "$TMP"
    # Push every 5 min — keeps Lightning's view fresh without burning commits
    sleep 300
done
