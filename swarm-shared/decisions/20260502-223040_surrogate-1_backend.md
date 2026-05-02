# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient, with deterministic sibling-repo sharding for write throughput.

### Changes
1. Add `bin/list-files.py` — single Mac-side script that lists one date folder via `list_repo_tree(recursive=False)` and emits `file-list.json` (path + size + sha256 stub). Embed this list in training/shard scripts so Lightning training does **zero API calls** during data load.
2. Update `bin/dataset-enrich.sh` to accept an optional `FILE_LIST` path; if provided, iterate the local list and download each file via CDN (`https://huggingface.co/datasets/.../resolve/main/...`) with `curl`/`requests` (no auth). Keep fallback to `load_dataset` for compatibility.
3. Add deterministic sibling-repo routing for uploads: `repo = f"axentx/surrogate-1-training-pairs-{hash(slug) % 5}"` (5 siblings = 640 commits/hr aggregate). Use existing repo as default when sibling missing (graceful fallback).
4. Add small `lib/cdn.py` helper with retry/backoff and 360s wait on 429.
5. Update README with usage and the HF CDN bypass note.

### Why this is highest value
- Eliminates HF API 429 during ingestion/training (the biggest recurring failure).
- Keeps shard workers fast and isolated (CDN unlimited vs 1000 req/5min API limit).
- Adds write throughput headroom via sibling repos without changing semantics.
- Fits in <2h: ~60 lines of new code + small script updates.

---

## Code Snippets

### 1) `bin/list-files.py`
```python
#!/usr/bin/env python3
"""
List files in a date folder of axentx/surrogate-1-training-pairs (non-recursive)
and emit file-list.json for CDN-only ingestion.

Usage:
  python bin/list-files.py --date 2026-05-02 --out file-list.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--repo", default=REPO, help="Dataset repo")
    args = parser.parse_args()

    api = HfApi()
    # Single API call; do not recurse.
    files = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)

    entries = []
    for f in files:
        if getattr(f, "type", None) == "file" or (hasattr(f, "size") and f.size is not None):
            entries.append({
                "path": f.path if hasattr(f, "path") else f.get("path", str(f)),
                "size": f.size if hasattr(f, "size") else f.get("size", 0),
            })

    payload = {
        "repo": args.repo,
        "date": args.date,
        "files": entries,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, indent=2)

    print(f"Wrote {len(entries)} entries to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-files.py
```

---

### 2) `lib/cdn.py`
```python
import time
import requests
from typing import Optional

def download_via_cdn(repo: str, path: str, out_path: str, max_retries: int = 5) -> bool:
    """
    Download a dataset file via public CDN (no auth). Retries with backoff.
    On 429, waits 360s before retry (HF rate-limit policy).
    """
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code == 429:
                    wait = 360
                    print(f"429 rate-limited, waiting {wait}s (attempt {attempt})", flush=True)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return True
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"Attempt {attempt} failed: {e}. Retrying in {wait}s", flush=True)
            time.sleep(wait)
    return False
```

---

### 3) Update `bin/dataset-enrich.sh` (minimal diff)
Add near top:
```bash
# Optional pre-computed file list (JSON from bin/list-files.py).
# If provided, workers will use CDN downloads instead of HF API streaming.
FILE_LIST="${FILE_LIST:-}"
```

In worker loop, add branch:
```bash
process_file() {
  local rel_path="$1"
  local workdir="$2"
  local out="$3"

  if [ -n "$FILE_LIST" ] && [ -f "$FILE_LIST" ]; then
    # CDN path: use lib/cdn.py (python) or curl if preferred.
    python3 -c "
import sys, tempfile, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from lib.cdn import download_via_cdn
repo='axentx/surrogate-1-training-pairs'
tmp = tempfile.mktemp(suffix='.parquet')
ok = download_via_cdn(repo, '$rel_path', tmp)
if not ok:
    sys.exit(1)
# project to {prompt,response} and append to $out
# (keep existing projection logic here)
" || return 1
  else
    # existing streaming path (datasets.load_dataset)
    python3 -c "
from datasets import load_dataset
# ... existing projection logic ...
" || return 1
  fi
}
```

(Keep existing projection/dedup/upload logic unchanged.)

---

### 4) Deterministic sibling-repo routing for uploads
Add small helper in `lib/upload.py` (or extend existing dedup/upload logic):
```python
import hashlib

def sibling_repo_for_slug(slug: str, n_siblings: int = 5, primary: str = "axentx/surrogate-1-training-pairs") -> str:
    """
    Deterministically pick sibling repo for write sharding.
    Returns primary if sibling index == 0 (so primary remains default).
    """
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    idx = h % n_siblings  # 0..4
    if idx == 0:
        return primary
    return f"{primary}-sibling{idx}"
```

Use in upload path:
```python
target_repo = sibling_repo_for_slug(slug)
# push to target_repo (fallback to primary on 404/403 if sibling repo doesn't exist)
```

---

### 5) Training script integration (Lightning)
Embed file list in `train.py`:
```python
import json
import os

# Mac produces file-list.json and bundles it with the Lightning run
_FILE_LIST = os.getenv("FILE_LIST_JSON", "file-list.json")
if os.path.exists(_FILE_LIST):
    with open(_FILE_LIST) as f:
        file_index = json.load(f)
    TRAIN_FILES = [f"{file_index['repo']}/{entry['path']}" for entry in file_index["files"]]
else:
    TRAIN_FILES = None  # fallback to HF API listing (slower/risk 429)
```

Then in Lightning `Dataset` class, use CDN-only downloads when `TRAIN_FILES` is present (via `lib/cdn.download_via_cdn`). This ensures **
