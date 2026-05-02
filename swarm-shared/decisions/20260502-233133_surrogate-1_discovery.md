# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_files` with a deterministic pre-flight snapshot + CDN-only fetches. This eliminates HF API rate limits (429), pyarrow CastError on mixed schemas, and prevents 7 GB runner OOM from parquet decode.

---

### Steps (1h 45m total)

1. **Add snapshot generator** (`bin/make-snapshot.py`) — run once per date folder (Mac or coordinator job). Uses `list_repo_tree(recursive=False)` per subfolder to avoid pagination. Outputs `snapshot-{date}.json` containing `{repo, path, sha, size, cdn_url}` for every parquet file.
2. **Refactor `bin/dataset-enrich.sh`** to accept `SNAPSHOT_FILE` (or date) and iterate over the snapshot list instead of calling `load_dataset`. Each shard filters by `hash(slug) % 16 == SHARD_ID`.
3. **Use CDN URLs** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with `requests` + `pyarrow.parquet` for zero-auth, zero-API data loading.
4. **Keep dedup via `lib/dedup.py`** unchanged (central md5 store).
5. **Update workflow** to pass snapshot artifact (or date) to matrix jobs; optionally generate snapshot in a lightweight “coordinator” job and `upload-artifact` for the 16 shards.

---

### Code Snippets

#### 1) Snapshot generator (run on Mac / cron / coordinator)

`bin/make-snapshot.py`
```python
#!/usr/bin/env python3
"""
Generate deterministic snapshot for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  python bin/make-snapshot.py --date 2026-05-02 --out snapshot-2026-05-02.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO = "datasets/axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (optional for public repo tree)")
    args = parser.parse_args()

    api = HfApi(token=args.token)
    base_path = f"batches/public-merged/{args.date}"

    entries = []
    # list top-level folders (e.g. 2026-05-02/part-00000, ...)
    try:
        tree = api.list_repo_tree(repo_id=REPO, path=base_path, recursive=False)
    except Exception as exc:
        print(f"Failed to list tree at {base_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    for node in tree:
        if node.type != "file":
            continue
        path = f"{base_path}/{node.path}"
        entries.append(
            {
                "repo": REPO,
                "path": path,
                "sha": getattr(node, "sha", None),
                "size": getattr(node, "size", None),
                "cdn_url": f"https://huggingface.co/{REPO}/resolve/main/{path}",
            }
        )

    snapshot = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": args.date,
        "base_path": base_path,
        "count": len(entries),
        "entries": entries,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    print(f"Wrote {len(entries)} entries to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/make-snapshot.py
```

---

#### 2) Updated worker script (core logic)

`bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# Updated: uses snapshot + CDN fetches instead of load_dataset(list_repo_files(...))
set -euo pipefail

# Required env
: "${HF_TOKEN:?HF_TOKEN required for dedup store writes}"
: "${SHARD_ID:?SHARD_ID (0-15) required}"
: "${SNAPSHOT_FILE:?Path to snapshot JSON required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH}"

# Date folder derived from snapshot or arg
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
OUTPUT_DIR="batches/public-merged/${DATE}"
mkdir -p "${OUTPUT_DIR}"

TIMESTAMP=$(date -u +%H%M%S)
OUTFILE="${OUTPUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

echo "[$(date -u)] Shard ${SHARD_ID} starting, snapshot=${SNAPSHOT_FILE}, out=${OUTFILE}"

python3 "${SCRIPT_DIR}/worker.py" \
  --shard-id "${SHARD_ID}" \
  --shard-total 16 \
  --snapshot "${SNAPSHOT_FILE}" \
  --out "${OUTFILE}"

echo "[$(date -u)] Shard ${SHARD_ID} finished, wrote ${OUTFILE}"
```

---

#### 3) Python worker (CDN-only fetches)

`bin/worker.py`
```python
#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# local
from lib.dedup import DedupStore

CDN_TIMEOUT = 30

def hash_slug(obj: Dict[str, Any]) -> int:
    # Deterministic shard assignment from content (or filename)
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return int(hashlib.md5(payload.encode()).hexdigest(), 16)

def fetch_parquet_rows(cdn_url: str, columns: List[str] = ("prompt", "response")) -> List[Dict[str, Any]]:
    """Download parquet via CDN and project to {prompt,response}."""
    resp = requests.get(cdn_url, timeout=CDN_TIMEOUT)
    resp.raise_for_status()
    with open("/tmp/tmp.parquet", "wb") as f:
        f.write(resp.content)

    table = pq.read_table("/tmp/tmp.parquet", columns=columns if columns else None)
    df = table.to_pandas()
    # Normalize column names
    df = df.rename(columns={c: c.lower() for c in df.columns})
    records = df.to_pandas().to_dict(orient="records")
    # Ensure prompt/response exist (fill missing)
    for r in records:
        r.setdefault("prompt", "")
        r.setdefault("response", "")
    return records

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--shard-total", type=int, default=16)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.snapshot, "r", encoding="utf-8") as f:
        snap = json.load(f)

    entries = snap["entries"]
    dedup = DedupStore()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with open(args.out, "w", encoding="utf-8") as out_f:
        for ent in tqdm(entries, desc=f"Shard {args.shard_id}"):
            cdn_url = ent["cdn_url"]
            try:
                rows = fetch_parquet_rows(cdn_url, columns=["prompt", "response"])
            except Exception as exc:
                print(f"Failed to fetch {cdn_url}: {exc
