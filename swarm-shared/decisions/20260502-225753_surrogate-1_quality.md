# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes
1. Add `bin/list-date-files.py` — single Mac-side script that calls `list_repo_tree(path, recursive=False)` for one date folder, saves JSON of file paths.
2. Update `bin/dataset-enrich.sh` to accept an optional file-list JSON; if provided, workers read paths from JSON (no `list_repo_*` calls) and download via raw CDN URLs (`resolve/main/...`).
3. Add retry/backoff for CDN downloads and deterministic shard assignment by `slug-hash % 16`.
4. Keep SQLite dedup store usage unchanged.

### Why this is highest value
- Eliminates recursive `list_repo_files` and per-file API calls during ingestion → removes 429 risk.
- CDN downloads bypass auth rate limits and are not counted against HF API quotas.
- Pre-listing once per date folder on the Mac (after rate-limit window) makes Lightning training scripts able to run with zero API calls (embed the same JSON in train.py).
- Minimal code change, <2h to ship.

---

## Code Snippets

### 1) bin/list-date-files.py
```python
#!/usr/bin/env python3
"""
Pre-flight file lister for a single date folder in axentx/surrogate-1-training-pairs.
Usage:
  HF_TOKEN=... python bin/list-date-files.py 2026-05-01 > filelist-2026-05-01.json
"""
import json
import os
import sys
from huggingface_hub import HfApi

REPO = "datasets/axentx/surrogate-1-training-pairs"

def main():
    if len(sys.argv) != 2:
        print("Usage: list-date-files.py <YYYY-MM-DD>", file=sys.stderr)
        sys.exit(1)

    date = sys.argv[1]
    api = HfApi(token=os.getenv("HF_TOKEN"))

    # list only immediate files in the date folder (no recursion)
    entries = api.list_repo_tree(
        repo_id=REPO,
        path=date,
        repo_type="dataset",
        recursive=False,
    )

    files = [e.path for e in entries if e.type == "file"]
    out = {
        "date": date,
        "repo": REPO,
        "files": sorted(files),
    }
    json.dump(out, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-date-files.py
```

---

### 2) bin/dataset-enrich.sh (updated)
```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Worker script for GitHub Actions matrix shard.
# Usage (via workflow):
#   SHARD_ID=0 SHARD_TOTAL=16 FILELIST=filelist-2026-05-01.json bash bin/dataset-enrich.sh

set -euo pipefail
export PYTHONUNBUFFERED=1

: "${SHARD_ID:?required}"
: "${SHARD_TOTAL:?required}"
: "${HF_TOKEN:?required}"
: "${FILELIST:-}"          # optional pre-listed JSON; if absent, fallback to API (not recommended)

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
OUTDIR="batches/public-merged/${DATE}"
mkdir -p "$OUTDIR"
TS=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TS}.jsonl"

# Dedup store (central SQLite) - unchanged
DEDUP_DB="/opt/axentx/surrogate-1/lib/dedup.db"
export DEDUP_DB

python3 - "$SHARD_ID" "$SHARD_TOTAL" "$FILELIST" <<'PY'
import json
import hashlib
import os
import sqlite3
import sys
import time
import requests
from pathlib import Path

SHARD_ID = int(sys.argv[1])
SHARD_TOTAL = int(sys.argv[2])
FILELIST = sys.argv[3] if sys.argv[3] not in ("", "None") else None

REPO = "datasets/axentx/surrogate-1-training-pairs"
DATE = os.getenv("DATE", "")
OUTFILE = os.getenv("OUTFILE", "")
DEDUP_DB = os.getenv("DEDUP_DB", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")

CDN_BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main"

HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

def deterministic_shard(path: str, total: int) -> int:
    slug = path.rsplit(".", 1)[0]
    h = hashlib.md5(slug.encode()).hexdigest()
    return int(h, 16) % total

def list_via_api():
    # fallback: use API once per worker (avoid recursive)
    from huggingface_hub import HfApi
    api = HfApi(token=HF_TOKEN)
    entries = api.list_repo_tree(
        repo_id=REPO,
        path=DATE,
        repo_type="dataset",
        recursive=False,
    )
    return sorted([e.path for e in entries if e.type == "file"])

if FILELIST:
    with open(FILELIST) as f:
        manifest = json.load(f)
    files = manifest["files"]
else:
    files = list_via_api()

# Filter to this shard
files = [p for p in files if deterministic_shard(p, SHARD_TOTAL) == SHARD_ID]

# Dedup helpers
def init_db():
    conn = sqlite3.connect(DEDUP_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def is_seen(conn, md5):
    cur = conn.execute("SELECT 1 FROM seen WHERE md5 = ?", (md5,))
    return cur.fetchone() is not None

def mark_seen(conn, md5):
    conn.execute("INSERT OR IGNORE INTO seen (md5) VALUES (?)", (md5,))
    conn.commit()

def download_with_retry(url, max_retries=5, backoff=1.0):
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise RuntimeError("unreachable")

# Minimal schema projection: keep only prompt/response fields if present.
# If file is JSONL, parse lines; if parquet, read via pyarrow and project.
import io
try:
    import pyarrow.parquet as pq
    import pyarrow as pa
    HAS_PARQUET = True
except Exception:
    HAS_PARQUET = False

def parse_file(content, path):
    path_l = path.lower()
    if path_l.endswith(".jsonl"):
        for line in content.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
            response = obj.get("response") or obj.get("output") or obj.get("completion")
            if prompt is not None and response is not None:
                yield {"prompt": str(prompt), "response": str(response)}
    elif HAS_PARQUET and path_l.endswith(".parquet"):
        try:
            table = pq.read_table(io.BytesIO(content))
            df = table.to_pandas()
            for _, row in df.iterrows():
                prompt = row.get("prompt") or row.get("input") or row.get("text")
                response = row.get("response") or row.get("output") or row.get("completion")
                if prompt is not None and response is not None:
                    yield {"prompt": str(prompt), "response": str(response)}
        except Exception:
            return
    else:
        # Unknown format — skip
        return

def run():
    if not OUTFILE:
        print("OUTFILE not set
