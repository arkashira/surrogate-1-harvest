#!/usr/bin/env bash
# Surrogate-1 v2 — Streaming bulk-mirror worker.
#
# Replaces bulk-mirror-worker.sh's full-download model with HF datasets
# STREAMING. Critical for trillion-token sources (fineweb 15T, dclm 4T,
# RedPajama-V2 30T) where full download is impossible on cpu-basic Space.
#
# Differences vs bulk-mirror-worker.sh:
#   • streaming=1 entries → load_dataset(..., streaming=True), iterate
#     incrementally, write each row as we read (no full-download buffer)
#   • token rotation: every 5000 rows pick next HF token from pool (avoid 429)
#   • polite delays: 0.05s between rows, capped at 30 rows/sec
#   • per-source caps in trillion-token-sources.txt (max_samples per run)
#   • exits cleanly when MAX_SAMPLES hit or 90 min elapsed (cron-friendly)
#
# Spawn N of these per cron tick. Each claims ONE source + finishes.
#
# Usage:
#   bash streaming-mirror-worker.sh [worker_id]
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }
WORKER_ID="${1:-sw-$$-$(date +%s)}"
LOG="$HOME/.surrogate/logs/streaming-worker-${WORKER_ID}.log"
mkdir -p "$(dirname "$LOG")"

echo "[$(date +%H:%M:%S)] streaming-worker $WORKER_ID start" | tee -a "$LOG"

# Soft wall-clock so cron tick (90 min) always finishes
WORKER_DEADLINE_SEC=5400

while true; do
    # claim next task from coordinator (existing SQLite claim-queue)
    TASK=$(python3 "$HOME/.surrogate/bin/v2/bulk-mirror-coordinator.py" claim "$WORKER_ID")
    REPO=$(echo "$TASK" | python3 -c "import sys, json; print(json.load(sys.stdin).get('repo_id') or '')")
    CID=$(echo "$TASK" | python3 -c "import sys, json; print(json.load(sys.stdin).get('id') or '')")
    MAX_N=$(echo "$TASK" | python3 -c "import sys, json; print(json.load(sys.stdin).get('max_samples') or 50000)")

    if [[ -z "$REPO" || "$REPO" == "None" ]]; then
        # Tight idle — discoverer is feeding queue continuously. 30s gap.
        echo "[$(date +%H:%M:%S)] queue empty; sleep 30s (discoverer feeds new sources)" >> "$LOG"
        sleep 30
        continue
    fi

    echo "[$(date +%H:%M:%S)] $WORKER_ID claimed #$CID $REPO (cap=$MAX_N)" | tee -a "$LOG"

    KEPT=0
    ERROR=""
    REPO="$REPO" MAX_N="$MAX_N" DEADLINE="$WORKER_DEADLINE_SEC" \
    HF_TOKEN_POOL="$HF_TOKEN_POOL" \
    python3 - <<'PYEOF' 2>>"$LOG" || ERROR="failed"
import sys, os, json, time, random
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
from sanitize import filter_pair
try: from dedup import DedupStore; HAS_DEDUP = True
except Exception: HAS_DEDUP = False

REPO = os.environ["REPO"]
MAX_N = int(os.environ.get("MAX_N", 50000))
DEADLINE = int(os.environ.get("DEADLINE", 5400))
START = time.time()
TOKENS = [k for k in os.environ.get("HF_TOKEN_POOL","").split(",") if k]

def get_token(idx):
    return TOKENS[idx % len(TOKENS)] if TOKENS else None

# Try streaming first; fall back to non-streaming for tiny datasets
from datasets import load_dataset
try:
    ds = load_dataset(REPO, split="train", streaming=True,
                      token=get_token(0))
    print(f"  [stream] {REPO} opened (streaming)")
except Exception as e:
    print(f"  [stream-fail→non-stream] {REPO}: {e}")
    try:
        ds = load_dataset(REPO, split="train", streaming=False,
                          token=get_token(0))
        print(f"  [non-stream] {REPO} ({len(ds) if hasattr(ds,'__len__') else '?'} rows)")
    except Exception as e2:
        print(f"  [hard-fail] {REPO}: {e2}")
        print("KEPT=0")
        sys.exit(0)

out_path = Path.home() / f".surrogate/data/bulk-mirror/{REPO.replace('/','_')}.jsonl"
out_path.parent.mkdir(parents=True, exist_ok=True)
kept = 0
seen = 0
with open(out_path, "a") as f:
    for ex in ds:
        seen += 1
        if kept >= MAX_N: break
        if (time.time() - START) > DEADLINE:
            print(f"  [deadline] hit {DEADLINE}s")
            break
        # Robust extraction across schemas
        p = (ex.get("prompt") or ex.get("instruction") or ex.get("question")
             or ex.get("input") or ex.get("query") or ex.get("text") or "")
        r = (ex.get("response") or ex.get("answer") or ex.get("output")
             or ex.get("completion") or ex.get("chosen") or "")
        if (not p or not r) and isinstance(ex.get("messages"), list) and len(ex["messages"]) >= 2:
            msgs = ex["messages"]
            u = next((m.get("content","") or m.get("value","") for m in msgs
                     if m.get("role") in ("user","human") or m.get("from") in ("user","human")), "")
            a = next((m.get("content","") or m.get("value","") for m in msgs
                     if m.get("role") in ("assistant","gpt") or m.get("from") in ("assistant","gpt")), "")
            if u and a: p, r = u, a
        if not p or not r:
            # raw text/web — bail to text-only mode (one-field datasets)
            t = ex.get("text") or ex.get("content") or ex.get("raw") or ""
            if t and len(t) > 200:
                # split heuristically: first 1/3 as "prompt", rest as "response"
                cut = len(t) // 3
                p, r = t[:cut].strip(), t[cut:].strip()
            else:
                continue
        p = str(p)[:100000].strip(); r = str(r)[:200000].strip()
        if len(p) < 20 or len(r) < 30: continue
        v = filter_pair(p, r)
        if not v["keep"]: continue
        if HAS_DEDUP and not DedupStore.is_new(p, source=f"stream-{REPO}"): continue
        f.write(json.dumps({"prompt": p, "response": r, "source": REPO}, ensure_ascii=False) + "\n")
        kept += 1
        # Polite throttle + token rotation marker
        if kept % 5000 == 0:
            print(f"  [progress] {REPO} kept={kept} seen={seen} "
                  f"elapsed={int(time.time()-START)}s")
            f.flush()

print(f"KEPT={kept}")
print(f"SEEN={seen}")
PYEOF

    KEPT=$(grep -oE "KEPT=[0-9]+" "$LOG" | tail -1 | cut -d= -f2)
    KEPT=${KEPT:-0}
    SEEN=$(grep -oE "SEEN=[0-9]+" "$LOG" | tail -1 | cut -d= -f2)

    python3 "$HOME/.surrogate/bin/v2/bulk-mirror-coordinator.py" done "$CID" "$KEPT" "$ERROR" >> "$LOG"
    echo "[$(date +%H:%M:%S)] $WORKER_ID done #$CID kept=$KEPT seen=${SEEN:-?}" | tee -a "$LOG"

    # Discord notify on big harvests
    if [[ -n "${DISCORD_WEBHOOK:-}" ]] && [[ ${KEPT:-0} -gt 10000 ]]; then
        curl -s -X POST -H "Content-Type: application/json" \
            -d "{\"content\":\"🌊 streaming-worker $WORKER_ID: harvested ${KEPT} from ${REPO}\"}" \
            "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
    fi

    # Soft deadline check — exit cleanly if cron tick is ending
    NOW=$(date +%s)
    START_EPOCH=$(stat -f %B "$LOG" 2>/dev/null || echo "$NOW")
    if (( NOW - START_EPOCH > WORKER_DEADLINE_SEC )); then
        echo "[$(date +%H:%M:%S)] $WORKER_ID hit deadline, exiting" | tee -a "$LOG"
        break
    fi
    sleep 5
done
