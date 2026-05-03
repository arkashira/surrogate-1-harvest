# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.sh`** → **`bin/dataset-enrich.py`**
   - Single API call (after rate-limit window) to `list_repo_tree(path, recursive=False)` for one date folder.
   - Save file list to `manifest.json`.
   - Worker loads manifest and downloads via **CDN URLs only** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — no Authorization header, bypasses `/api/` rate limits.
   - Per-record schema projection to `{prompt, response}` only; drop all other fields.
   - Deterministic slug hash → `SHARD_ID` routing (same 1/16 split as current).
   - Output: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **`lib/dedup.py`** (unchanged API, minor tweak)
   - Keep central md5 dedup store; expose `is_duplicate(md5) -> bool` and `add(md5) -> None`.
   - Accept optional `batch_add` for speed.

3. **`.github/workflows/ingest.yml`**
   - Matrix `shard_id: [0..15]`.
   - Each job runs `python bin/dataset-enrich.py --shard ${{ matrix.shard_id }} --date-folder <YYYY-MM-DD>`.
   - No recursive `list_repo_files`; single tree call per job (or reuse manifest artifact if you want to optimize further).

4. **`requirements.txt`**
   - Add `requests` (CDN downloads), keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`.

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-first, CDN-bypass enrichment worker for surrogate-1.
Usage:
  python bin/dataset-enrich.py --shard 0 --date-folder 2026-05-03
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, List

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download

HF_REPO = "axentx/surrogate-1-training-pairs"
API = HfApi()

# Project record to canonical {prompt, response}
def project_record(rec: Dict[str, Any]) -> Dict[str, str]:
    prompt = rec.get("prompt") or rec.get("input") or rec.get("question") or ""
    response = rec.get("response") or rec.get("output") or rec.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def build_manifest(date_folder: str, out_path: Path) -> List[str]:
    """Single API tree call -> manifest.json with file paths for date_folder."""
    tree = API.list_repo_tree(
        repo_id=HF_REPO,
        path=date_folder,
        recursive=False,
        repo_type="dataset",
    )
    files = [
        f.rfilename
        for f in tree
        if f.type == "file" and f.rfilename.lower().endswith((".jsonl", ".parquet", ".json"))
    ]
    manifest = {"date_folder": date_folder, "files": files}
    out_path.write_text(json.dumps(manifest, indent=2))
    return files

def download_via_cdn(repo: str, path: str, local_path: Path) -> None:
    """CDN download — no Authorization header, bypasses /api/ rate limits."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def process_file(path: str, shard_id: int, total_shards: int, dedup) -> List[Dict[str, str]]:
    """Load file, project schema, filter by shard, dedup by md5."""
    local = Path("tmp") / path.replace("/", "_")
    download_via_cdn(HF_REPO, path, local)

    pairs = []
    if path.endswith(".parquet"):
        table = pq.read_table(local)
        # Avoid mixed-schema CastError: read as pyarrow then convert to dicts
        df = table.to_pandas()
        for _, row in df.iterrows():
            rec = row.to_dict()
            canonical = project_record(rec)
            if not canonical["prompt"] or not canonical["response"]:
                continue
            text = canonical["prompt"] + "\n" + canonical["response"]
            if shard_id != (int(md5_hex(text), 16) % total_shards):
                continue
            if dedup.is_duplicate(md5_hex(text)):
                continue
            pairs.append(canonical)
            dedup.add(md5_hex(text))
    else:
        # JSONL / JSON
        content = local.read_text(encoding="utf-8")
        for line in content.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            canonical = project_record(rec)
            if not canonical["prompt"] or not canonical["response"]:
                continue
            text = canonical["prompt"] + "\n" + canonical["response"]
            if shard_id != (int(md5_hex(text), 16) % total_shards):
                continue
            if dedup.is_duplicate(md5_hex(text)):
                continue
            pairs.append(canonical)
            dedup.add(md5_hex(text))
    return pairs

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True, help="Shard ID 0..15")
    parser.add_argument("--date-folder", required=True, help="e.g. 2026-05-03")
    parser.add_argument("--workers", type=int, default=1, help="Parallel download workers (future)")
    args = parser.parse_args()

    TOTAL_SHARDS = 16
    if not (0 <= args.shard < TOTAL_SHARDS):
        print(f"Invalid shard {args.shard}. Must be 0..{TOTAL_SHARDS - 1}", file=sys.stderr)
        sys.exit(1)

    from lib.dedup import DedupStore

    dedup = DedupStore()
    out_dir = Path("batches/public-merged") / args.date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    out_file = out_dir / f"shard{args.shard}-{ts}.jsonl"

    manifest_path = Path("manifest.json")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        files = manifest["files"]
    else:
        files = build_manifest(args.date_folder, manifest_path)

    total = 0
    for fpath in files:
        try:
            pairs = process_file(fpath, args.shard, TOTAL_SHARDS, dedup)
            if pairs:
                with open(out_file, "a", encoding="utf-8") as fh:
                    for p in pairs:
                        fh.write(json.dumps(p, ensure_ascii=False) + "\n")
                total += len(pairs)
        except Exception as exc:
            print(f"Error processing {fpath}: {exc}", file=sys.stderr)

    print(f"Shard {args.shard}: wrote {total} pairs to {out_file}")

if __name__ == "__main__":
    main()
```

### `lib/dedup.py`

```python
import sqlite3
from pathlib import Path
from typing import List

DB_PATH = Path("dedup_store.db")

class DedupStore:
    def __init__(self
