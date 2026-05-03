# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.sh`** → **`bin/dataset-enrich.py`**
   - Drop `load_dataset(streaming=True)` for heterogeneous repos.
   - Single Mac-side `list_repo_tree` call → save `manifest.json` (date folder + file list).
   - Worker uses **CDN-only downloads** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — zero API calls during training.
   - Per-file download → project to `{prompt, response}` only at parse time → prevents pyarrow CastError.
   - Deterministic shard assignment via `hash(slug) % 16 == SHARD_ID`.
   - Central dedup via existing `lib/dedup.py` (SQLite md5 store).
   - Output: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **`lib/dedup.py`** (no change) — keep as central md5 store.

3. **`.github/workflows/ingest.yml`**
   - Update matrix runner to invoke `python bin/dataset-enrich.py` with `SHARD_ID`, `HF_TOKEN`, `DATE`.
   - Keep 16 parallel runners.

4. **`requirements.txt`**
   - Add `requests` (CDN downloads), keep `huggingface_hub` (only for `list_repo_tree` + upload).

---

## Code Snippets

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.
Usage:
  SHARD_ID=0 python bin/dataset-enrich.py --date 2026-05-03 --hf-token $HF_TOKEN
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download

REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"

# Project only these keys; drop everything else to avoid mixed-schema issues
TARGET_KEYS = {"prompt", "response"}

def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str, shard_id: int, total_shards: int = 16) -> bool:
    return slug_hash(slug) % total_shards == shard_id

def list_date_files(api: HfApi, date: str) -> list[str]:
    """Single API call: list files in date folder (non-recursive)."""
    try:
        tree = api.list_repo_tree(repo_id=REPO, path=date, recursive=False)
        return [item.rfilename for item in tree if item.rfilename.endswith((".jsonl", ".parquet"))]
    except Exception as e:
        print(f"Error listing repo tree: {e}", file=sys.stderr)
        return []

def load_manifest(date: str) -> list[str]:
    manifest_path = Path("manifest") / f"{date}.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    return []

def save_manifest(date: str, files: list[str]) -> None:
    manifest_path = Path("manifest")
    manifest_path.mkdir(exist_ok=True)
    manifest_path.joinpath(f"{date}.json").write_text(json.dumps(files, indent=2))

def download_via_cdn(path: str, dest: Path) -> None:
    url = f"{BASE_CDN}/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)

def parse_file_to_pairs(local_path: Path) -> list[dict]:
    """Parse a single file and project to {prompt, response} only."""
    pairs = []
    try:
        if local_path.suffix == ".parquet":
            tbl = pq.read_table(local_path)
            cols = tbl.column_names
            # Keep only target keys if present; allow missing keys -> null
            for col in list(cols):
                if col not in TARGET_KEYS:
                    tbl = tbl.drop(col)
            df = tbl.to_pandas()
        else:  # jsonl
            lines = local_path.read_text().strip().splitlines()
            rows = [json.loads(l) for l in lines if l.strip()]
            df = pa.Table.from_pylist(rows).to_pandas()
            df = df[[c for c in df.columns if c in TARGET_KEYS]]

        for _, row in df.iterrows():
            prompt = row.get("prompt")
            response = row.get("response")
            if prompt is None or response is None:
                continue
            pairs.append({"prompt": str(prompt), "response": str(response)})
    except Exception as e:
        print(f"Parse error {local_path}: {e}", file=sys.stderr)
    return pairs

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--hf-token", required=True, help="HF write token")
    parser.add_argument("--shard-id", type=int, default=int(os.getenv("SHARD_ID", 0)))
    parser.add_argument("--total-shards", type=int, default=16)
    args = parser.parse_args()

    api = HfApi(token=args.hf_token)
    work_dir = Path("work") / args.date / f"shard{args.shard_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1) Manifest: list files once (single API call)
    files = load_manifest(args.date)
    if not files:
        files = list_date_files(api, args.date)
        if not files:
            print("No files found for date; exiting.")
            return
        save_manifest(args.date, files)

    # 2) Filter by shard
    my_files = [f for f in files if belongs_to_shard(f, args.shard_id, args.total_shards)]
    print(f"Shard {args.shard_id}: processing {len(my_files)} files")

    # 3) Import dedup store
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from lib.dedup import DedupStore
    dedup = DedupStore()

    all_pairs = []
    for rel_path in my_files:
        local_file = work_dir / Path(rel_path).name
        try:
            download_via_cdn(rel_path, local_file)
            pairs = parse_file_to_pairs(local_file)
            for p in pairs:
                # Deterministic content hash for dedup
                content = f"{p['prompt']}\n{p['response']}"
                md5 = hashlib.md5(content.encode()).hexdigest()
                if dedup.is_duplicate(md5):
                    continue
                dedup.add(md5)
                all_pairs.append(p)
        finally:
            if local_file.exists():
                local_file.unlink()

    if not all_pairs:
        print("No new pairs after dedup; skipping upload.")
        return

    # 4) Upload shard output
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_name = f"shard{args.shard_id}-{timestamp}.jsonl"
    out_path = Path("batches") / "public-merged" / args.date / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(p) for p in all_pairs))

    # Upload to HF dataset repo
    api.upload_file(
        path_or_fileobj=str(out_path),
        path_in_repo=str(out_path),
        repo_id=REPO,
        repo_type="dataset",
    )
    print(f"Uploaded {out_path} ({len(all_pairs)} pairs)")

if __name__ == "__main__":
    main()
```

### `lib/dedup.py` (unchanged — central md5 store)
```python
import sqlite3
from pathlib import Path

