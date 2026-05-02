# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers fully resilient.

### Changes
1. Add `bin/list-date-files.py` — single Mac-side script that calls `list_repo_tree` once per date folder, saves `file-list-<date>.json` (path + size + etag) to `batches/public-merged/<date>/`. This list is committed and reused by both GitHub Actions runners and Lightning training scripts, enabling **zero API calls** during data load.
2. Update `bin/dataset-enrich.sh` to accept an optional file-list JSON. If provided, workers iterate the local list and fetch via CDN (`/resolve/main/...`) with no `list_repo_files` or recursive API calls. Fallback to current behavior if no list (keeps cron safe).
3. Add `lib/cdn_stream.py` helper that downloads via CDN with retries and range requests; projects to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas).
4. Update README with the new workflow: run `list-date-files.py` after rate-limit window closes; shard workers use the committed list.

---

### 1) `bin/list-date-files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  python list-date-files.py --date 2026-04-29 --out batches/public-merged/2026-04-29/file-list-2026-04-29.json

Writes JSON:
{
  "date": "2026-04-29",
  "generated_at_utc": "...",
  "folder_prefix": "raw",
  "repo": "datasets/axentx/surrogate-1-training-pairs",
  "files": [
    {"path": "raw/2026-04-29/file1.parquet", "size": 12345, "etag": "abc..."},
    ...
  ]
}
"""

import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"

def list_date_files(date_str: str, folder_prefix: str = "raw"):
    api = HfApi()
    target_path = f"{folder_prefix}/{date_str}"
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=target_path,
        repo_type="dataset",
        recursive=False,
    )

    files = []
    for e in entries:
        if e.type == "file":
            files.append({
                "path": e.path,
                "size": e.size,
                "etag": getattr(e, "etag", None)
            })

    files.sort(key=lambda x: x["path"])
    return files

def main():
    parser = argparse.ArgumentParser(description="List date folder files for surrogate-1.")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--folder", default="raw", help="Top folder under dataset (default: raw)")
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print("ERROR: --date must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    files = list_date_files(args.date, folder_prefix=args.folder)
    payload = {
        "date": args.date,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "folder_prefix": args.folder,
        "repo": REPO_ID,
        "files": files,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-date-files.py
```

---

### 2) `lib/cdn_stream.py`

```python
import json
import tempfile
import time
from pathlib import Path
from typing import Iterator, Tuple

import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter, Retry

HF_CDN = "https://huggingface.co/datasets"

def cdn_parquet_url(repo: str, filepath: str) -> str:
    # /resolve/main/ bypasses API auth and rate limits (CDN tier)
    return f"{HF_CDN}/{repo}/resolve/main/{filepath}"

def _make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session

def stream_cdn_parquet_to_dicts(
    repo: str,
    filepath: str,
    max_rows: int = None,
    session: requests.Session = None,
) -> Iterator[dict]:
    """
    Download a parquet file via CDN and yield rows as dicts.
    Keeps memory low by using pyarrow and iterating record batches.
    Projects heterogeneous schemas to a consistent dict at parse time
    to avoid pyarrow CastError on mixed schemas.
    """
    url = cdn_parquet_url(repo, filepath)
    sess = session or _make_session()

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
        try:
            resp = sess.get(url, timeout=60)
            resp.raise_for_status()
            tmp.write(resp.content)
            tmp.flush()

            pf = pq.ParquetFile(tmp_path)
            count = 0
            for batch in pf.iter_batches(batch_size=1024):
                for row in batch.to_pylist():
                    # Project to consistent keys at parse time
                    row = _project_row(row)
                    yield row
                    count += 1
                    if max_rows is not None and count >= max_rows:
                        return
        finally:
            Path(tmp_path).unlink(missing_ok=True)

def _project_row(row: dict) -> dict:
    """
    Project heterogeneous schemas to {prompt, response} only.
    Avoids keeping extra fields that can cause schema mismatches downstream.
    """
    # Common patterns seen in HF datasets
    if "prompt" in row and "response" in row:
        return {"prompt": str(row["prompt"]), "response": str(row["response"])}
    if "instruction" in row and "output" in row:
        return {"prompt": str(row["instruction"]), "response": str(row["output"])}
    if "input" in row and "output" in row:
        return {"prompt": str(row["input"]), "response": str(row["output"])}
    if "text" in row:
        t = str(row["text"])
        return {"prompt": t, "response": t}
    # last resort: JSON dump
    return {"prompt": json.dumps(row, ensure_ascii=False), "response": ""}

def project_to_pair(row: dict) -> Tuple[str, str]:
    """Legacy helper for callers expecting (prompt, response)."""
    projected = _project_row(row)
    return projected["prompt"], projected["response"]
```

---

### 3) Updated `bin/dataset-enrich.sh`

Key changes:
- Accept optional `--file-list FILELIST.json`. If provided, workers read local list and fetch via CDN (no `load_dataset` recursive/list calls).
- Use `lib/cdn_stream.py` for CDN streaming with retries.
- Deterministic shard assignment by slug hash.
- Keep existing behavior as fallback.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Enrich public dataset shards and upload deduped pairs.
#
# Usage:
#   ./dataset-enrich.sh
