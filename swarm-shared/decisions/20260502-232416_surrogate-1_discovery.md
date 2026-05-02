# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Goal**: Eliminate runtime `load_dataset(streaming=True)` and recursive `list_repo_files` from `bin/dataset-enrich.sh`. Replace with deterministic pre-flight snapshots and CDN-only fetches to avoid HF API rate limits and schema heterogeneity issues.

---

### Steps (ordered, 90–120 min total)

1. **Add snapshot utility** (`bin/make-snapshot.py`)  
   - Run once on Mac (or in CI before the 16-shard matrix)  
   - Calls `list_repo_tree(path=date, recursive=False)` via `huggingface_hub`  
   - Emits `snapshot/<date>/file-list.json` (flat list of `{path, size, sha, date}`)  
   - Exits 0 only if snapshot created; fails fast if API unavailable  
   - On 429: prints `Retry-After` and exits non-zero so CI can retry job after window clears

2. **Update `bin/dataset-enrich.sh`**  
   - Accept optional `FILE_LIST` env var pointing to snapshot JSON  
   - If `FILE_LIST` provided: skip all `list_repo_files`/`load_dataset` discovery; iterate paths from JSON  
   - Fetch each file via CDN URL:  
     ```
     https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>
     ```
     (no Authorization header; CDN tier has much higher limits)  
   - Keep existing per-record schema projection to `{prompt,response}` only; drop extra columns  
   - Write output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` unchanged

3. **Update GitHub Actions (`ingest.yml`)**  
   - Add an initial “snapshot” job (or step) that runs `bin/make-snapshot.py` and uploads `file-list.json` as an artifact  
   - Pass `FILE_LIST` path to each matrix shard via `env.FILE_LIST`  
   - Ensure shards download the artifact before running enrichment  
   - If snapshot step fails (e.g., 429), fail workflow fast and retry later

4. **Hardening**  
   - Add retry/backoff for CDN downloads (429/5xx) with exponential backoff (max 5 retries)  
   - Validate file extension (`.jsonl`, `.parquet`, `.csv`) before fetch; skip unknown  
   - Log skipped/duplicate counts and per-shard summary to stdout for observability  
   - Skip zero-size files and handle truncated reads gracefully

5. **Cleanup (within 2h)**  
   - Remove any `load_dataset(streaming=True)` imports/usage from the repo  
   - Remove recursive `list_repo_files` calls  
   - Keep only the new snapshot + CDN path

---

### Code snippets

#### `bin/make-snapshot.py`
```python
#!/usr/bin/env python3
"""
Create a deterministic pre-flight snapshot for a date folder.
Usage:
    DATE=2024-06-01 python3 bin/make-snapshot.py
"""
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO = os.environ.get("REPO", "axentx/surrogate-1-training-pairs")
DATE = os.environ.get("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_DIR = os.environ.get("OUT_DIR", "snapshot")
OUT_FILE = os.path.join(OUT_DIR, DATE, "file-list.json")

os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)

api = HfApi()

try:
    tree = api.list_repo_tree(repo=REPO, path=DATE, recursive=False)
except Exception as e:
    # Fallback: list root and filter by date prefix
    try:
        tree = api.list_repo_tree(repo=REPO, path="", recursive=False)
        tree = [t for t in tree if t.path.startswith(f"{DATE}/")]
    except Exception as e2:
        print(f"Failed to list repo: {e2}", file=sys.stderr)
        sys.exit(1)

files = []
for t in tree:
    if getattr(t, "type", None) != "file":
        continue
    files.append({
        "path": t.path,
        "size": getattr(t, "size", None),
        "sha": getattr(t, "sha", None),
        "date": DATE,
    })

with open(OUT_FILE, "w") as f:
    json.dump(files, f, indent=2)

print(f"Wrote {len(files)} files to {OUT_FILE}")
sys.exit(0)
```

Make executable:
```bash
chmod +x bin/make-snapshot.py
```

---

#### Updated `bin/dataset-enrich.sh` (key changes only)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT_DIR="batches/public-merged/${DATE}"
TS="$(date +%H%M%S)"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"
FILE_LIST="${FILE_LIST:-}"

mkdir -p "$(dirname "${OUT_FILE}")"

python3 - <<PY
import os, json, hashlib, sys, time, requests, pyarrow as pa, pyarrow.parquet as pq
from io import BytesIO

REPO = os.environ.get("REPO", "$REPO")
DATE = os.environ.get("DATE", "$DATE")
SHARD_ID = int(os.environ.get("SHARD_ID", "$SHARD_ID"))
TOTAL_SHARDS = int(os.environ.get("TOTAL_SHARDS", "$TOTAL_SHARDS"))
OUT_FILE = os.environ.get("OUT_FILE", "$OUT_FILE")
FILE_LIST = os.environ.get("FILE_LIST", "$FILE_LIST")

def deterministic_shard(path, n):
    return (int(hashlib.md5(path.encode()).hexdigest(), 16) % n)

def download_cdn(path):
    url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60 * (2 ** attempt)))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")

def extract_pairs(content, path):
    ext = os.path.splitext(path)[1].lower()
    pairs = []
    if ext == ".jsonl":
        for line in content.decode("utf-8", errors="replace").strip().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt is not None and response is not None:
                pairs.append({"prompt": str(prompt), "response": str(response)})
    elif ext == ".parquet":
        try:
            table = pq.read_table(pa.BufferReader(content))
            cols = table.column_names
            prompt_col = next((c for c in ["prompt", "input", "question"] if c in cols), None)
            response_col = next((c for c in ["response", "output", "answer"] if c in cols), None)
            if prompt_col and response_col:
                for i in range(table.num_rows):
                    row = table.slice(i, 1).to_pydict()
                    pairs.append({
                        "prompt": str(row[prompt_col][0]),
                        "response": str(row[response_col][0])
                    })
        except Exception:
            pass
    elif ext == ".csv":
        lines = content.decode("utf-8", errors="replace").strip().splitlines()
        if not lines:
            return pairs
        header = [h.strip().lower() for h in lines[0].split(",")]
        prompt_idx = next((i for i, h in enumerate(header) if h in ("prompt", "input", "
