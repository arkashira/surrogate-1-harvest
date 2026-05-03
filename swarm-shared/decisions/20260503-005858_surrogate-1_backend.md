# surrogate-1 / backend

## Highest-Value Incremental Improvement (≤2h)

**Goal**: Eliminate HF API rate-limit failures during ingestion and prevent OOM in the HF Space by switching to CDN-only fetches and per-folder tree listing.

**Why this wins**:
- Removes `list_repo_files` recursive calls that trigger 429 (1000 req/5min).
- Uses CDN URLs (`resolve/main/...`) for data downloads — no Authorization header, bypasses API rate limits.
- Prevents `load_dataset(streaming=True)` on heterogeneous schemas (avoids PyArrow CastError).
- Keeps per-shard deterministic assignment and dedup behavior unchanged.
- Fits in <2h: one small script change + one workflow flag.

---

## Implementation Plan

1. **Add a pre-list step** (run once per cron/workflow, before shards start):
   - Use `huggingface_hub.list_repo_tree(path, recursive=False)` per date folder.
   - Save flat file list to `file-list.json` as an artifact.
   - If API is throttled, fallback to a cached list or wait (respect 360s backoff).

2. **Modify `bin/dataset-enrich.sh`**:
   - Accept a file list (or date folder) as input.
   - For each assigned file (by `slug-hash % 16 == SHARD_ID`):
     - Download via CDN: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.
     - Parse locally, project to `{prompt, response}` only.
     - Compute md5, check central dedup store, emit normalized JSONL.
   - Upload shard output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

3. **Update workflow** (`ingest.yml`):
   - Add a setup job that produces `file-list.json` artifact.
   - Pass `date_folder` and `file-list.json` to the 16-shard matrix.
   - Ensure `HF_TOKEN` only used for repo metadata + upload (not per-file reads).

4. **Safety & observability**:
   - Log per-file CDN HTTP status; retry 429 with 360s sleep.
   - Keep existing dedup logic (`lib/dedup.py`) unchanged.
   - Keep filename pattern `batches/public-merged/{date}/shard<N>-{HHMMSS}.jsonl`.

---

## Code Changes

### 1) New helper: `bin/list-date-folder.sh`

```bash
#!/usr/bin/env bash
# bin/list-date-folder.sh
# Usage: list-date-folder.sh <repo> <date_folder> [out.json]
# Example: list-date-folder.sh axentx/surrogate-1-training-pairs 2026-05-03 file-list.json

set -euo pipefail
REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
OUT="${3:-file-list.json}"

python3 - "$REPO" "$DATE_FOLDER" "$OUT" <<'PY'
import json
import os
import sys
import time
from huggingface_hub import HfApi, RepositoryError

REPO = sys.argv[1]
DATE_FOLDER = sys.argv[2]
OUT = sys.argv[3]

api = HfApi()

# Retry wrapper for 429
def list_tree_with_backoff(*args, **kwargs):
    wait = 60
    for attempt in range(5):
        try:
            return api.list_repo_tree(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                print(f"[rate-limit] attempt {attempt+1} failed, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                wait = min(wait * 2, 360)
                continue
            raise
    raise RuntimeError("Exhausted retries for HF API rate limit")

# List only the target date folder (non-recursive)
entries = list_tree_with_backoff(
    repo_id=REPO,
    path=DATE_FOLDER,
    recursive=False
)

files = [e.rfilename for e in entries if not e.rfilename.endswith("/")]
print(f"Found {len(files)} files in {REPO}/{DATE_FOLDER}")

with open(OUT, "w") as f:
    json.dump({"date_folder": DATE_FOLDER, "files": files}, f, indent=2)
print(f"Wrote {OUT}")
PY
```

Make executable:
```bash
chmod +x bin/list-date-folder.sh
```

---

### 2) Update worker: `bin/dataset-enrich.sh` (CDN fetch + schema projection)

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Runs per shard. Inputs:
#   SHARD_ID (0-15)
#   DATE_FOLDER (e.g., 2026-05-03)
#   FILE_LIST (path to JSON produced by list-date-folder.sh)
#   HF_REPO (default: axentx/surrogate-1-training-pairs)
#   HF_TOKEN (for upload only)

set -euo pipefail
export HF_REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
export DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
export SHARD_ID="${SHARD_ID:?required}"
export FILE_LIST="${FILE_LIST:?required}"
export OUT_DIR="output/${DATE_FOLDER}"
mkdir -p "$OUT_DIR"

TIMESTAMP=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

python3 - "$SHARD_ID" "$FILE_LIST" "$OUT_FILE" "$HF_REPO" "$DATE_FOLDER" <<'PY'
import json
import hashlib
import os
import sys
import time
import requests
from pathlib import Path

SHARD_ID = int(sys.argv[1])
FILE_LIST = sys.argv[2]
OUT_FILE = sys.argv[3]
HF_REPO = sys.argv[4]
DATE_FOLDER = sys.argv[5]

# Central dedup store (SQLite) — keep existing behavior
DEDUP_DB = Path("lib/dedup.db")
DEDUP_DB.parent.mkdir(exist_ok=True)

import sqlite3
conn = sqlite3.connect(str(DEDUP_DB))
conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")
conn.commit()

def is_duplicate(md5):
    cur = conn.execute("SELECT 1 FROM seen WHERE md5 = ?", (md5,))
    return cur.fetchone() is not None

def mark_seen(md5):
    conn.execute("INSERT OR IGNORE INTO seen (md5) VALUES (?)", (md5,))
    conn.commit()

def normalize_record(raw):
    # Heuristic projection to {prompt, response}
    # Keep minimal; schema heterogeneity handled by per-file parsing
    if isinstance(raw, dict):
        prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
        response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
        return {"prompt": str(prompt), "response": str(response)}
    return {"prompt": "", "response": ""}

with open(FILE_LIST) as f:
    data = json.load(f)

files = data.get("files", [])
print(f"Shard {SHARD_ID}: processing {len(files)} files from {DATE_FOLDER}")

cdn_base = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

out_lines = []
for i, rfilename in enumerate(files):
    # Deterministic shard assignment by slug-hash
    slug_hash = abs(hash(rfilename)) % (2**31)
    if (slug_hash % 16) != SHARD_ID:
        continue

    url = f"{cdn_base}/{rfilename}"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 429:
            print(f"[cdn] 429 on {url}, sleeping 360s")
            time.sleep(360)
            resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[cdn] failed {url}: {e}")
        continue

    # Try parquet first, fallback to json/jsonl
    import io
    import pyarrow.parquet as pq

