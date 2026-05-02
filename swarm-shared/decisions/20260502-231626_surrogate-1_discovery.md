# surrogate-1 / discovery

## Final Synthesis: Deterministic Pre-flight Snapshot + CDN-only Ingestion

**Core insight (unified):** Replace runtime HF API listing and streaming with a single deterministic snapshot (JSON) and CDN-only fetches. This eliminates 429s, makes shard workers idempotent, and keeps quota/load predictable.

**Resolved contradictions in favor of correctness + actionability:**
- Use **one-level-deep bounded listing** (not full recursive) to avoid pagination/timeouts while still capturing expected shard outputs.
- Keep shard workers **stateless** (no cross-run dedup state) but **idempotent** via snapshot + deterministic hash-based shard assignment.
- Prefer **CDN `https://huggingface.co/datasets/.../resolve/main/...`** for all training/shard fetches; reserve HF API only for the single snapshot call.
- Do **not** use `load_dataset(..., streaming=True)` on heterogeneous repos (avoids Pyarrow CastError).
- Reuse existing Lightning Studio instead of recreating; launcher should check for running studios to avoid quota waste.

---

## Single Implementation Plan (≤2h)

| Step | Owner | Time | Command / Code |
|------|-------|------|----------------|
| 1. Snapshot listing script | Me | 15m | `bin/list-snapshot.sh` |
| 2. Embed snapshot in training script | Me | 20m | `train.py` reads `snapshots/{date}.json` |
| 3. Update shard worker for snapshot mode | Me | 20m | `bin/dataset-enrich.sh` optional snapshot path |
| 4. Lightning Studio reuse guard | Me | 10m | launcher checks running studios |
| 5. Test run (local dry-run + 1 shard) | Me | 30m | verify CDN-only paths and no `list_repo_*` during load |
| 6. Commit + push | Me | 5m | |

Total: ~1h40m (buffer included).

---

## 1) Snapshot listing script (Mac orchestration)

`bin/list-snapshot.sh`
```bash
#!/usr/bin/env bash
# Usage: HF_TOKEN=... ./bin/list-snapshot.sh axentx/surrogate-1-training-pairs main 2026-05-01
# Produces: snapshots/2026-05-01.json  (list of file paths under that date folder)
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
BRANCH="${2:-main}"
DATE="${3:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots"
OUTFILE="${OUTDIR}/${DATE}.json"

mkdir -p "${OUTDIR}"

python3 - "$REPO" "$BRANCH" "$DATE" "$OUTFILE" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo, branch, date_folder, outfile = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
api = HfApi()

# Top-level non-recursive to avoid pagination explosion.
entries = api.list_repo_tree(repo, revision=branch, path=date_folder, recursive=False)

files = []
for e in entries:
    if e.type == "file":
        files.append(e.path)
    elif e.type == "dir":
        # One-level deeper only (bounded)
        sub = api.list_repo_tree(repo, revision=branch, path=e.path, recursive=False)
        for s in sub:
            if s.type == "file":
                files.append(s.path)

# Deterministic ordering
files.sort()
with open(outfile, "w") as f:
    json.dump({"repo": repo, "branch": branch, "date": date_folder, "files": files}, f, indent=2)

print(f"Snapshot written: {outfile} ({len(files)} files)")
PY

echo "Snapshot created: ${OUTFILE}"
```

Make executable:
```bash
chmod +x bin/list-snapshot.sh
```

---

## 2) Training script: CDN-only data loader using snapshot

`train.py` (excerpt)
```python
import json, os, sys
from pathlib import Path
import requests
from datasets import Dataset, Features, Value

HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
BRANCH = os.getenv("BRANCH", "main")

def load_snapshot(date: str) -> list[str]:
    snapshot_path = Path("snapshots") / f"{date}.json"
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot missing: {snapshot_path}")
    with open(snapshot_path) as f:
        data = json.load(f)
    return data["files"]

def cdn_fetch(file_path: str) -> bytes:
    # CDN URL — no Authorization header (bypasses /api/ rate limits)
    url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

def build_dataset_from_snapshot(date: str):
    files = load_snapshot(date)
    pairs = []
    for fp in files:
        # Only expected shard outputs: batches/public-merged/<date>/shardN-*.jsonl
        if not fp.endswith(".jsonl"):
            continue
        raw = cdn_fetch(fp)
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            # Expect {prompt:..., response:...} minimal schema
            try:
                obj = json.loads(line)
                pairs.append({"prompt": obj["prompt"], "response": obj["response"]})
            except Exception:
                continue
    return Dataset.from_list(pairs, features=Features({
        "prompt": Value("string"), "response": Value("string")
    }))

if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-05-01"
    ds = build_dataset_from_snapshot(date)
    print(f"Loaded {len(ds)} pairs from CDN snapshot for {date}")
    # Continue training with ds ...
```

Notes:
- No `load_dataset(..., streaming=True)` on heterogeneous repos (avoids Pyarrow CastError).
- No `list_repo_files` or recursive API calls during training (CDN-only after snapshot).

---

## 3) Shard worker: optional snapshot mode (idempotent)

Update `bin/dataset-enrich.sh` to optionally accept a snapshot file to limit scope (makes retries deterministic and faster).

`bin/dataset-enrich.sh` (excerpt additions)
```bash
#!/usr/bin/env bash
# ... existing header ...

# Optional snapshot mode: if SNAPSHOT_FILE is set, only process listed files.
# This makes shard retries deterministic and avoids listing API during run.
process_with_snapshot() {
  local snapshot="$1"
  if [[ ! -f "$snapshot" ]]; then
    echo "Snapshot not found: $snapshot" >&2
    exit 1
  fi
  # Extract file list for this shard deterministically:
  # shard N of TOTAL_SHARDS reads lines assigned by hash(slug) % TOTAL_SHARDS
  python3 - "$snapshot" "$SHARD_ID" "$TOTAL_SHARDS" <<'PY'
import json, hashlib, sys
from pathlib import Path
snapshot, shard_id, total_shards = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(snapshot) as f:
    data = json.load(f)
files = data.get("files", [])
for fp in files:
    # Deterministic shard assignment by slug (filename without extension)
    slug = Path(fp).stem
    bucket = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % total_shards
    if bucket == shard_id:
        print(fp)
PY
}

if [[ -n "${SNAPSHOT_FILE:-}" ]]; then
  echo "Running in snapshot mode: ${SNAPSHOT_FILE}"
  FILE_LIST=$(mktemp)
  process_with_snapshot "${SNAPSHOT_FILE}" > "${FILE_LIST}"
else
  # fallback to existing behavior (stream from repo)
  FILE_LIST=$(mktemp)
  python3 - "$REPO" "$BRANCH" "$DATE" "$SHARD_ID" "$TOTAL_SHARDS" > "${FILE_LIST}" <<'PY'
# ... existing shard-assignment logic ...
PY
fi

# Process files
