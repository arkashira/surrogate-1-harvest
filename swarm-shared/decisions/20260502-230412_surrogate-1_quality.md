# surrogate-1 / quality

### Final Consolidated Implementation (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

---

### Concrete Changes
1. **Add `bin/list-date-files.py`** — Mac-side script that calls `list_repo_tree` once per date folder, saves `date-files.json`, and embeds it into training/shard workflows.
2. **Add `lib/cdn_stream.py`** — lightweight CDN fetcher with retries, range requests, per-record projection to `{prompt, response}`, and support for Parquet + JSONL.
3. **Update `bin/dataset-enrich.sh`** to accept an optional file-list JSON and stream via CDN URLs (bypassing `datasets.load_dataset` for heterogeneous schemas).
4. **Update GitHub Actions matrix** to pass the pre-computed file list to each shard so all runners use the same snapshot and avoid per-run `list_repo_tree` API calls.

---

### Code Snippets

#### 1) `bin/list-date-files.py`
```python
#!/usr/bin/env python3
"""
Usage:
  python bin/list-date-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out date-files.json

Lists files under a date folder (non-recursive per subfolder) and produces:
{
  "date": "2026-05-02",
  "repo": "axentx/surrogate-1-training-pairs",
  "folders": {
    "public-raw/2026-05-02": ["file1.parquet", ...],
    "batches/public-merged/2026-05-02": ["shard0-120000.jsonl", ...]
  },
  "files": [
    {"path": "public-raw/2026-05-02/file1.parquet", "size": 12345},
    ...
  ]
}
"""
import argparse
import json
import os
import sys

from huggingface_hub import HfApi

def list_date_files(repo_id: str, date: str, out_path: str):
    api = HfApi()
    prefix = f"{date}"
    entries = api.list_repo_tree(repo_id, path=prefix, recursive=False)

    folders = {}
    files = []

    for e in entries:
        if e.type == "directory":
            subpath = e.path
            try:
                subentries = api.list_repo_tree(repo_id, path=subpath, recursive=False)
            except Exception as ex:
                print(f"WARN: failed to list {subpath}: {ex}", file=sys.stderr)
                continue
            folders[subpath] = [se.path.split("/")[-1] for se in subentries if se.type == "file"]
            for se in subentries:
                if se.type == "file":
                    files.append({"path": se.path, "size": getattr(se, "size", None)})
        elif e.type == "file":
            files.append({"path": e.path, "size": getattr(e, "size", None)})

    payload = {
        "date": date,
        "repo": repo_id,
        "folders": folders,
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) if os.path.dirname(out_path) else ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} file entries to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List files for a date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo id")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()
    list_date_files(args.repo, args.date, args.out)
```

#### 2) `lib/cdn_stream.py`
```python
import json
import time
from pathlib import Path
from typing import Iterator, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_url(repo: str, path: str) -> str:
    return CDN_TEMPLATE.format(repo=repo, path=path)

def robust_get(url: str, retries: int = 5, backoff: float = 1.0) -> requests.Response:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", backoff * (2 ** attempt)))
                print(f"Rate-limited (429). Waiting {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            wait = backoff * (2 ** attempt)
            print(f"Request failed ({exc}), retry {attempt}/{retries} in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")

def stream_parquet_cdn(repo: str, path: str, max_records: int = None) -> Iterator[Dict[str, Any]]:
    url = cdn_url(repo, path)
    resp = robust_get(url)
    table = pq.read_table(pa.BufferReader(resp.content))
    has_prompt = "prompt" in table.column_names
    has_response = "response" in table.column_names
    rows = 0
    for i in range(table.num_rows):
        if max_records is not None and rows >= max_records:
            break
        row = {}
        if has_prompt:
            row["prompt"] = table["prompt"][i].as_py()
        if has_response:
            row["response"] = table["response"][i].as_py()
        yield row
        rows += 1

def stream_jsonl_cdn(repo: str, path: str, max_records: int = None) -> Iterator[Dict[str, Any]]:
    url = cdn_url(repo, path)
    resp = robust_get(url)
    rows = 0
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if max_records is not None and rows >= max_records:
            break
        record = json.loads(line)
        yield {
            "prompt": record.get("prompt"),
            "response": record.get("response"),
        }
        rows += 1

def stream_cdn(repo: str, path: str, max_records: int = None) -> Iterator[Dict[str, Any]]:
    if path.endswith(".parquet"):
        yield from stream_parquet_cdn(repo, path, max_records=max_records)
    elif path.endswith(".jsonl"):
        yield from stream_jsonl_cdn(repo, path, max_records=max_records)
    else:
        raise ValueError(f"Unsupported file type: {path}")
```

#### 3) `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE_FILES="${1:-}"
OUT_DIR="${2:-./enriched}"

mkdir -p "$OUT_DIR"

if [[ -n "$DATE_FILES" && -f "$DATE_FILES" ]]; then
  echo "Using pre-listed files from $DATE_FILES"
  FILES=$(jq -r '.files[].path' "$DATE_FILES")
else
  echo "ERROR: file list JSON required (run bin/list-date-files.py first)"
  exit 1
fi

for path in $FILES; do
  url="https://huggingface.co/datasets/${REPO}/resolve/main/${path}"
  echo "Streaming $path from CDN..."
  python3 -c "
import sys
from lib.cdn_stream import stream_cdn
repo = '${REPO}'
for rec in stream_cdn(repo, '${path}'):
    print(rec)
" > "${OUT_DIR}/$(basename "$path" | sed 's/\.[^.]*$/.jsonl/')"
done

echo "Enrichment complete. Outputs in $OUT_DIR"
```

---

###
