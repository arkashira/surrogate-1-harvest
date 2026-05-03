# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.sh`** → **`bin/dataset-enrich.py`**
   - Single API call (post-rate-limit window) to `list_repo_tree(path, recursive=False)` for one date folder → save `manifest.json`
   - Worker uses **CDN-only fetches** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization header → bypasses `/api/` rate limits
   - Per-file schema projection to `{prompt, response}` only at parse time → prevents `pyarrow.CastError` on heterogeneous repos
   - Deterministic shard assignment via `hash(slug) % 16` → no collisions
   - Central dedup via existing `lib/dedup.py` SQLite store

2. **`requirements.txt`**
   - Add `requests` (CDN downloads), keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`

3. **`.github/workflows/ingest.yml`**
   - Update to run `python bin/dataset-enrich.py` instead of shell script
   - Pass `SHARD_ID` and `DATE` as env vars

4. **`lib/dedup.py`** (no change) — reuse existing SQLite store

---

## Code Snippets

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.
Usage: python bin/dataset-enrich.py --shard 0 --date 2026-05-03
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import list_repo_tree, hf_hub_download

# Local
from lib.dedup import DedupStore

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
HF_USER = "axentx"
HF_DATASET = "surrogate-1-training-pairs"
CDN_BASE = f"https://huggingface.co/datasets/{HF_USER}/{HF_DATASET}/resolve/main"
DATE_FMT = "%Y-%m-%d"
BATCH_SIZE = 500  # rows per chunk before flush

def shard_for_slug(slug: str, n_shards: int = 16) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % n_shards

def list_date_folder(date_str: str) -> list[str]:
    """Single API call to list one date folder (non-recursive)."""
    try:
        tree = list_repo_tree(
            repo_id=f"{HF_USER}/{HF_DATASET}",
            path=date_str,
            recursive=False,
        )
        return [item.rfilename for item in tree if item.type == "file"]
    except Exception as e:
        print(f"[WARN] list_repo_tree failed: {e}", file=sys.stderr)
        return []

def download_via_cdn(path: str, retries: int = 3) -> bytes:
    """CDN-only fetch — no Authorization header, bypasses API rate limits."""
    url = f"{CDN_BASE}/{path}"
    for i in range(retries):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            if i == retries - 1:
                raise
            wait = (2 ** i) + (hash(url) % 3)
            print(f"[WARN] CDN fetch failed ({e}), retry {i+1}/{retries} in {wait}s", file=sys.stderr)
            time.sleep(wait)

def parse_file_to_rows(content: bytes, filename: str) -> list[dict]:
    """
    Project heterogeneous schemas to {prompt, response} only.
    Supports: parquet, jsonl, json.
    """
    rows = []
    try:
        # Parquet
        table = pq.read_table(pa.BufferReader(content))
        df = table.to_pandas()
    except Exception:
        # JSON/JSONL fallback
        try:
            text = content.decode("utf-8")
            # JSONL
            if "\n" in text.strip():
                import json as jsonlib
                df = pa.Table.from_pylist([
                    jsonlib.loads(line) for line in text.strip().split("\n") if line.strip()
                ]).to_pandas()
            else:
                # JSON
                import json as jsonlib
                df = pa.Table.from_pylist([jsonlib.loads(text)]).to_pandas()
        except Exception as e:
            print(f"[WARN] cannot parse {filename}: {e}", file=sys.stderr)
            return []

    # Normalize column names
    col_map = {}
    for c in df.columns:
        cl = c.lower().strip()
        if cl in {"prompt", "question", "input", "instruction", "text"}:
            col_map[c] = "prompt"
        elif cl in {"response", "answer", "output", "completion", "assistant", "generation"}:
            col_map[c] = "response"

    if "prompt" not in col_map.values() or "response" not in col_map.values():
        # Try heuristic: first text col = prompt, second = response
        text_cols = [c for c in df.columns if df[c].dtype == "object"]
        if len(text_cols) >= 2:
            col_map[text_cols[0]] = "prompt"
            col_map[text_cols[1]] = "response"
        else:
            print(f"[WARN] cannot project schema for {filename}", file=sys.stderr)
            return []

    # Rename and select
    df = df.rename(columns={k: v for k, v in col_map.items() if v in {"prompt", "response"}})
    if "prompt" not in df.columns or "response" not in df.columns:
        return []

    for _, row in df.iterrows():
        prompt = str(row["prompt"]).strip()
        response = str(row["response"]).strip()
        if not prompt or not response:
            continue
        rows.append({
            "prompt": prompt,
            "response": response,
            "source_file": filename,
        })
    return rows

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True, help="SHARD_ID 0..15")
    parser.add_argument("--date", type=str, default=datetime.now(timezone.utc).strftime(DATE_FMT))
    parser.add_argument("--out-dir", type=str, default="batches/public-merged")
    args = parser.parse_args()

    shard_id = args.shard
    date_str = args.date
    out_dir = Path(args.out_dir) / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    dedup = DedupStore()
    dedup.setup()

    # 1) List date folder once (single API call)
    print(f"[INFO] Listing {date_str}...")
    files = list_date_folder(date_str)
    if not files:
        print(f"[WARN] No files found for {date_str}", file=sys.stderr)
        return

    # Save manifest for reproducibility / training script embedding
    manifest_path = out_dir / f"manifest-shard{shard_id}.json"
    with open(manifest_path, "w") as f:
        json.dump({"date": date_str, "shard": shard_id, "files": files}, f, indent=2)

    # 2) Process files belonging to this shard
    my_files = [f for f in files if shard_for_slug(f) == shard_id]
    print(f"[INFO] Shard {shard_id}: processing {len(my_files)}/{len(files)} files")

    rows_buffer = []
    out_file = out_dir / f"shard{shard_id}-{datetime.utcnow().strftime('%H%M%S')}.jsonl"

    for filepath in my_files:
        try:
            content = download_via_cdn(filepath)
            file_rows = parse_file_to_rows(content, filepath)
            for row in file_rows:
                # Dedup by content hash (central store)
                digest = hashlib.md5
