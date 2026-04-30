#!/usr/bin/env bash
# Bulk mirror worker — claims dataset from coordinator, mirrors + enriches + uploads.
# Spawn N of these on HF Space; each runs in its own loop, no duplication.
#
# Usage: bash bulk-mirror-worker.sh [worker_id]

set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a
WORKER_ID="${1:-w-$$-$(date +%s)}"
LOG="$HOME/.surrogate/logs/bulk-worker-${WORKER_ID}.log"
mkdir -p "$(dirname "$LOG")"

echo "[$(date +%H:%M:%S)] worker $WORKER_ID start" | tee -a "$LOG"

# Loop forever, claiming + processing
while true; do
    # Claim next task
    TASK=$(python3 "$HOME/.surrogate/bin/v2/bulk-mirror-coordinator.py" claim "$WORKER_ID")
    REPO=$(echo "$TASK" | python3 -c "import sys, json; print(json.load(sys.stdin).get('repo_id') or '')")
    CID=$(echo "$TASK" | python3 -c "import sys, json; print(json.load(sys.stdin).get('id') or '')")
    MAX_N=$(echo "$TASK" | python3 -c "import sys, json; print(json.load(sys.stdin).get('max_samples') or 50000)")

    if [[ -z "$REPO" || "$REPO" == "None" ]]; then
        echo "[$(date +%H:%M:%S)] queue empty; sleep 30s (discoverer feeds)" >> "$LOG"
        sleep 30
        continue
    fi

    echo "[$(date +%H:%M:%S)] $WORKER_ID claimed #$CID $REPO (max=$MAX_N)" | tee -a "$LOG"

    # Run mirror (uses sanitizer + DedupStore + relevance filter via dataset-mirror.sh)
    KEPT=0
    ERROR=""
    HF_TOKEN="$HF_TOKEN" python3 - "$REPO" "$MAX_N" <<'PYEOF' 2>>"$LOG" || ERROR="failed"
import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
from sanitize import filter_pair
try: from dedup import DedupStore; HAS_DEDUP = True
except Exception: HAS_DEDUP = False

repo, max_n = sys.argv[1], int(sys.argv[2])
from datasets import load_dataset
try:
    ds = load_dataset(repo, split="train", streaming=True)
except Exception as e:
    print(f"  load fail: {e}")
    print(f"KEPT=0")
    sys.exit(0)

import time as _t
out_path = Path.home() / f".surrogate/data/bulk-mirror/{repo.replace('/','_')}.jsonl"
out_path.parent.mkdir(parents=True, exist_ok=True)
kept = 0
with open(out_path, "w") as f:
    for ex in ds:
        if kept >= max_n: break
        # Robust extraction
        p = (ex.get("prompt") or ex.get("instruction") or ex.get("question")
             or ex.get("input") or ex.get("query") or ex.get("text") or "")
        r = (ex.get("response") or ex.get("answer") or ex.get("output")
             or ex.get("completion") or ex.get("chosen") or "")
        if (not p or not r) and isinstance(ex.get("messages"), list) and len(ex["messages"]) >= 2:
            msgs = ex["messages"]
            u = next((m.get("content","") or m.get("value","") for m in msgs if m.get("role") in ("user","human") or m.get("from") in ("user","human")), "")
            a = next((m.get("content","") or m.get("value","") for m in msgs if m.get("role") in ("assistant","gpt") or m.get("from") in ("assistant","gpt")), "")
            if u and a: p, r = u, a
        if not p or not r: continue
        p, r = str(p)[:100000].strip(), str(r)[:200000].strip()
        if len(p) < 20 or len(r) < 30: continue
        v = filter_pair(p, r)
        if not v["keep"]: continue
        if HAS_DEDUP and not DedupStore.is_new(p, source=f"bulk-{repo}"): continue
        f.write(json.dumps({"prompt": p, "response": r, "source": repo}, ensure_ascii=False) + "\n")
        kept += 1

print(f"KEPT={kept}")
PYEOF

    # Parse KEPT from python output
    KEPT=$(grep -oE "KEPT=[0-9]+" "$LOG" | tail -1 | cut -d= -f2)
    KEPT=${KEPT:-0}

    # Mark done in coordinator
    python3 "$HOME/.surrogate/bin/v2/bulk-mirror-coordinator.py" done "$CID" "$KEPT" "$ERROR" >> "$LOG"
    echo "[$(date +%H:%M:%S)] $WORKER_ID done #$CID kept=$KEPT" | tee -a "$LOG"

    # Brief pause to be gentle on HF API
    sleep 10
done
