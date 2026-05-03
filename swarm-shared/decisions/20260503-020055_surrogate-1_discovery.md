# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from the GitHub Actions matrix.
- Loads a pre-generated `manifest-YYYYMMDD.json` (produced once per day on the Mac orchestrator via a single `list_repo_tree` call) containing all file paths to ingest for that date.
- Assigns each file to a deterministic shard: `hash(slug) % SHARD_TOTAL == SHARD_ID`.
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, no API rate-limit pressure.
- Normalizes heterogeneous schemas to `{prompt, response}` only at parse time (avoids `pyarrow.CastError`).
- Deduplicates via the existing `lib/dedup.py` central md5 store.
- Emits `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` with one `{prompt, response}` pair per line; attribution moved into filename pattern, no extra columns.
- Exits with code 0 on success, non-zero on fatal failure (GitHub Actions will retry per matrix shard).

### Why this is the highest-value incremental improvement
- Eliminates HF API rate-limit risk during ingestion (CDN bypass).
- Removes `load_dataset(streaming=True)` schema heterogeneity failures.
- Keeps parallelism (16 shards) while bounding memory per runner (~7 GB).
- Reuses existing dedup store and output conventions — minimal blast radius.
- Can ship in <2h: single Python script + small workflow tweak.

---

## Code Snippets

### 1. `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 20260503 \
    --out-dir batches/public-merged
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import pyarrow as pa
import pyarrow.parquet as pq

from lib.dedup import DedupStore  # existing central md5 store

HF_CDN_ROOT = "https://huggingface.co/datasets"
RETRY_WAIT = 30
MAX_RETRIES = 5

def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % total

def cdn_download(url: str, timeout: int = 60) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=False)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(RETRY_WAIT * attempt)

def normalize_to_pair(raw, filename: str):
    """
    Project heterogeneous schemas to {prompt, response}.
    Accepts dict-like or pyarrow Table row.
    """
    d = dict(raw) if not isinstance(raw, dict) else raw

    # Common field names seen across repos
    prompt_keys = {"prompt", "instruction", "input", "question", "text"}
    response_keys = {"response", "output", "answer", "completion", "text"}

    prompt = None
    response = None

    for k in d:
        klow = k.lower()
        if klow in prompt_keys and prompt is None:
            prompt = str(d[k]).strip()
        elif klow in response_keys and response is None:
            response = str(d[k]).strip()

    # Fallbacks
    if prompt is None:
        prompt = ""
    if response is None:
        response = ""

    # If single text column, split heuristically (optional)
    if not prompt and not response and "text" in d:
        parts = str(d["text"]).strip().split("\n\n", 1)
        if len(parts) == 2:
            prompt, response = parts[0].strip(), parts[1].strip()
        else:
            prompt = parts[0].strip()
            response = ""

    # Attach attribution via filename pattern (no extra cols)
    return {"prompt": prompt, "response": response}

def process_parquet(content: bytes, dedup: DedupStore, out_f, filename_slug: str):
    try:
        table = pq.read_table(pa.BufferReader(content))
    except Exception as exc:
        # Skip unreadable parquet
        print(f"WARN: failed to read parquet {filename_slug}: {exc}", file=sys.stderr)
        return 0

    count = 0
    for batch in table.to_batches():
        for row in batch.to_pylist():
            pair = normalize_to_pair(row, filename_slug)
            text = json.dumps(pair, ensure_ascii=False)
            md5 = hashlib.md5(text.encode()).hexdigest()
            if dedup.seen(md5):
                continue
            out_f.write(text + "\n")
            count += 1
    return count

def process_jsonl(content: bytes, dedup: DedupStore, out_f, filename_slug: str):
    count = 0
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except Exception:
            continue
        pair = normalize_to_pair(raw, filename_slug)
        text = json.dumps(pair, ensure_ascii=False)
        md5 = hashlib.md5(text.encode()).hexdigest()
        if dedup.seen(md5):
            continue
        out_f.write(text + "\n")
        count += 1
    return count

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYYMMDD")
    parser.add_argument("--manifest", help="Path to manifest-YYYYMMDD.json")
    parser.add_argument("--out-dir", default="batches/public-merged")
    parser.add_argument("--shard-id", type=int, default=int(os.getenv("SHARD_ID", 0)))
    parser.add_argument("--shard-total", type=int, default=int(os.getenv("SHARD_TOTAL", 16)))
    args = parser.parse_args()

    if args.shard_total <= 0 or args.shard_id < 0 or args.shard_id >= args.shard_total:
        print("ERROR: invalid SHARD_ID/SHARD_TOTAL", file=sys.stderr)
        sys.exit(1)

    manifest_path = args.manifest or f"manifest-{args.date}.json"
    if not os.path.exists(manifest_path):
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path) as f:
        files = json.load(f)  # list of relative paths

    my_files = [
        p for p in files
        if deterministic_shard(p, args.shard_total) == args.shard_id
    ]

    ts = datetime.utcnow().strftime("%H%M%S")
    out_dir = Path(args.out_dir) / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{args.shard_id}-{ts}.jsonl"

    dedup = DedupStore()
    total_pairs = 0

    with out_path.open("w", encoding="utf-8") as out_f:
        for rel_path in my_files:
            url = f"{HF_CDN_ROOT}/{args.repo}/resolve/main/{rel_path}"
            slug = f"{args.repo}/{rel_path}"
            try:
                content = cdn_download(url)
            except Exception as exc:
                print(f"WARN: failed to download {url}: {exc}", file=sys.stderr)
                continue

            lower = rel_path.lower()
            try:
                if lower.endswith(".parquet"):
                    n = process_parquet(content, dedup, out_f, slug)
                elif lower.endswith(".jsonl"):

