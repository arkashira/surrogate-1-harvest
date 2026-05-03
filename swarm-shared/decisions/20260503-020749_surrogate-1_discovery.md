# surrogate-1 / discovery

## Implementation Plan (≤2 h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side HF API call) listing one date folder (e.g., `public-merged/2026-05-03/`) to avoid recursive `list_repo_files` and HF API rate limits.
- Deterministically assigns files to shards by `hash(slug) % SHARD_TOTAL`.
- Downloads assigned files via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` 429 limits).
- Streams JSONL/Parquet, projects to `{prompt, response}`, computes content hash for local dedup, and emits one shard output file:
  - `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
- Writes minimal stats to stdout for Actions logging.
- Keeps `lib/dedup.py` as the cross-run dedup helper (SQLite) but runs per-shard to avoid cross-shard coordination.

### Steps (timeboxed)

1. Inspect current `bin/dataset-enrich.sh` and `lib/dedup.py` (already known).  
2. Create `bin/dataset-enrich.py` with CDN-bypass + manifest ingestion.  
3. Add small helper `bin/build-file-list.py` (Mac-side) to generate `file-list.json` for a date folder.  
4. Update `.github/workflows/ingest.yml` to:
   - Generate/push `file-list.json` once (or reuse existing) before matrix.
   - Pass `FILE_LIST` artifact to each shard job.
   - Keep 16-shard matrix with `SHARD_ID`/`SHARD_TOTAL`.
5. Smoke-test locally with a small file list.

---

## Code Snippets

### 1) `bin/build-file-list.py` (run on Mac)

```python
#!/usr/bin/env python3
"""
Generate file-list.json for a date folder to avoid recursive HF API calls.
Usage:
  HF_TOKEN=... python bin/build-file-list.py \
    --repo axentx/surrogate-1-training-pairs \
    --folder batches/public-merged/2026-05-03 \
    --out file-list.json
"""
import argparse
import json
import os
import sys

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    # Non-recursive per folder to avoid pagination explosion
    entries = api.list_repo_tree(repo_id=args.repo, path=args.folder, recursive=False)
    files = [e.path for e in entries if e.type == "file"]
    payload = {
        "repo": args.repo,
        "folder": args.folder.rstrip("/"),
        "files": sorted(files),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/build-file-list.py
```

---

### 2) `bin/dataset-enrich.py` (worker)

```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 dataset ingestion.

Environment:
  SHARD_ID=0..15
  SHARD_TOTAL=16
  FILE_LIST=file-list.json
  HF_DATASET_REPO=axentx/surrogate-1-training-pairs
  RUN_TS=YYYYmmddHHMMSS  (optional; defaults to now)
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

from lib.dedup import DedupStore

CDN_BASE = "https://huggingface.co/datasets"
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB

def _hash_slug(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)

def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

def _date_from_folder(folder: str) -> str:
    # Expecting batches/public-merged/YYYY-MM-DD
    parts = folder.rstrip("/").split("/")
    if len(parts) >= 3:
        return parts[-1]
    return datetime.utcnow().strftime("%Y-%m-%d")

def _project_to_pair(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
    response = obj.get("response") or obj.get("output") or obj.get("answer")
    if not prompt or not response:
        return None
    if not isinstance(prompt, str) or not isinstance(response, str):
        return None
    return {"prompt": prompt.strip(), "response": response.strip()}

def _content_hash(pair: Dict[str, str]) -> str:
    payload = f"{pair['prompt']}\n{pair['response']}".encode()
    return hashlib.md5(payload).hexdigest()

def _download_cdn(url: str) -> Iterable[bytes]:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                yield chunk

def _process_jsonl(content: bytes, dedup: DedupStore) -> Iterable[Dict[str, str]]:
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        pair = _project_to_pair(obj)
        if not pair:
            continue
        h = _content_hash(pair)
        if dedup.seen(h):
            continue
        yield pair

def _process_parquet(content: bytes, dedup: DedupStore) -> Iterable[Dict[str, str]]:
    try:
        table = pq.read_table(pa.BufferReader(content))
    except Exception:
        return
    df = table.to_pandas()
    for _, row in df.iterrows():
        pair = _project_to_pair(row.to_dict())
        if not pair:
            continue
        h = _content_hash(pair)
        if dedup.seen(h):
            continue
        yield pair

def process_shard(
    repo: str,
    files: list[str],
    shard_id: int,
    shard_total: int,
    dedup: DedupStore,
) -> list[Dict[str, str]]:
    my_files = [
        f for f in files if _hash_slug(f) % shard_total == shard_id
    ]
    results: list[Dict[str, str]] = []
    for fp in tqdm(my_files, desc=f"Shard {shard_id}", unit="file"):
        url = f"{CDN_BASE}/{repo}/resolve/main/{fp}"
        try:
            content = b"".join(_download_cdn(url))
        except Exception as exc:
            print(f"Failed {fp}: {exc}", file=sys.stderr)
            continue

        if fp.endswith(".jsonl"):
            results.extend(_process_jsonl(content, dedup))
        elif fp.endswith(".parquet"):
            results.extend(_process_parquet(content, dedup))
        else:
            print(f"Skipping unsupported {fp}", file=sys.stderr)
    return results

def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    file_list_path = os.getenv("FILE_LIST", "file-list.json")
    repo = os
