# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Uses a **pre-generated manifest** (created once per `DATE` on the Mac orchestrator) to avoid recursive `list_repo_tree`/pagination and API rate limits
- Downloads files via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header during data transfer** (bypasses `/api/` 429 limits)
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids `pyarrow.CastError`)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Uses deterministic sharding by slug-hash; no cross-run state

---

### Steps (≤2h)

1. **Create `bin/gen-manifest.py`** (Mac orchestrator: `list_repo_tree` → `manifest-{DATE}.json`) — 20 min  
2. **Create `bin/dataset-enrich.py`** (manifest + CDN worker) — 60 min  
3. **Update `.github/workflows/ingest.yml`** to generate manifest once and pass to matrix workers — 20 min  
4. **Smoke test** (local + dry-run) — 20 min

---

### 1) `bin/gen-manifest.py` (Mac orchestrator)

```python
#!/usr/bin/env python3
"""
Generate manifest-{DATE}.json for a given DATE.

Usage:
  HF_TOKEN=hf_xxx \
  python bin/gen-manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

HF_API = "https://huggingface.co/api/datasets"

def list_repo_tree(repo: str, date: str, token: Optional[str] = None) -> list:
    """
    Return list of dicts: {"path": "...", "type": "file", "size": ...}
    for files under {date}/ in the repo.
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{HF_API}/{repo}/tree/main"
    params = {"recursive": "true", "path": date}
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    tree = resp.json()
    return [item for item in tree if item.get("type") == "file"]

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate manifest for a DATE folder.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="DATE folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output manifest JSON path")
    parser.add_argument("--hf-token", default=os.getenv("HF_TOKEN"))
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        sys.exit("Invalid DATE format; expected YYYY-MM-DD")

    token = args.hf_token
    items = list_repo_tree(args.repo, args.date, token)

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": [{"path": item["path"], "size": item.get("size", 0)} for item in items],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written to {out_path} ({len(items)} files)")

if __name__ == "__main__":
    main()
```

---

### 2) `bin/dataset-enrich.py` (CDN worker)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage:
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --manifest manifest-2026-05-03.json \
    --out-dir batches/public-merged
"""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

CDN_BASE = "https://huggingface.co/datasets"
DEFAULT_REPO = "axentx/surrogate-1-training-pairs"
CHUNK_SIZE = 8192
MAX_RETRIES = 5
RETRY_BACKOFF = 30  # seconds (after 429/503)

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_BASE}/{repo}/resolve/main/{path}"

def robust_get(url: str, token: Optional[str] = None) -> requests.Response:
    # Do NOT send Authorization header for CDN downloads (bypasses /api/ 429 limits)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, stream=True, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = RETRY_BACKOFF * attempt
                print(f"[{resp.status_code}] retry {attempt}/{MAX_RETRIES} in {wait}s: {url}", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except (requests.RequestException, requests.Timeout) as exc:
            wait = RETRY_BACKOFF * attempt
            print(f"[error] retry {attempt}/{MAX_RETRIES} in {wait}s: {exc}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch after {MAX_RETRIES} retries: {url}")

def file_md5(resp: requests.Response) -> str:
    h = hashlib.md5()
    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
        h.update(chunk)
    return h.hexdigest()

def parse_file(content_bytes: bytes, path: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous HF dataset files to [{prompt, response}].
    Supports: .parquet, .json, .jsonl, .csv
    """
    path_l = path.lower()
    records: List[Dict[str, str]] = []

    try:
        if path_l.endswith(".parquet"):
            tbl = pq.read_table(pa.BufferReader(content_bytes))
            df = tbl.to_pandas()
        elif path_l.endswith(".jsonl"):
            lines = [ln.strip() for ln in content_bytes.decode("utf-8").splitlines() if ln.strip()]
            df = pa.Table.from_pylist([json.loads(ln) for ln in lines]).to_pandas()
        elif path_l.endswith(".json"):
            data = json.loads(content_bytes.decode("utf-8"))
            if isinstance(data, dict):
                df = pd.DataFrame([data])
            else:
                df = pd.DataFrame(data)
        elif path_l.endswith(".csv"):
            df = pd.read_csv(pa.BufferReader(content_bytes))
        else:
            # fallback: try parquet then jsonl
            try:
               
