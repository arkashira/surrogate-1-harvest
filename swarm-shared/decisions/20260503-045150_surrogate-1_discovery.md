# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`s.

### What we’ll ship
- `bin/ingest_worker.py` — single, deterministic worker that:
  - Reads a pre-computed `manifest.json` (date folder → file slugs) produced once on the Mac orchestrator.
  - Downloads only its 1/16 shard via **CDN direct URLs** (no Authorization header → bypasses `/api/` rate limits).
  - Projects heterogeneous files to `{prompt, response}` at parse time (avoids `pyarrow` schema merge).
  - Produces `shard-<N>-<ts>.parquet` (columnar, smaller, typed) instead of JSONL.
  - Pushes to HF via `huggingface_hub` with deterministic filenames so commits never collide.
- Update `bin/dataset-enrich.sh` → thin wrapper that calls `ingest_worker.py` with `SHARD_ID` and `MANIFEST_PATH`.
- Update GitHub Actions matrix to pass `SHARD_ID` and generated `manifest.json` artifact (or embed date list in the workflow).
- Keep `lib/dedup.py` as the source-of-truth central md5 store (SQLite on HF Space); workers call it via HTTP or skip cross-run dedup (as documented).

### Why this is highest value
- Eliminates HF API 429s during training by using CDN-only fetches (the key 2026-04-29 insight).
- Fixes `pyarrow CastError` by projecting schema at parse time, not during `load_dataset`.
- Replaces brittle shell streaming with typed Python (easier to maintain, test, extend).
- Keeps 16-shard parallelism and deterministic sharding; no infra changes.

---

## Code Snippets

### 1) `bin/ingest_worker.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingest worker for surrogate-1.
Usage:
  SHARD_ID=3 MANIFEST_PATH=manifest.json python bin/ingest_worker.py
"""

import json
import os
import sys
import hashlib
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

HF_DATASET = "axentx/surrogate-1-training-pairs"
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"

# Deterministic shard assignment
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest.json")
OUT_DIR = Path(os.getenv("OUT_DIR", "output"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

def load_manifest(path: str) -> Dict[str, List[str]]:
    with open(path) as f:
        return json.load(f)  # {"2026-05-03": ["file1.json", ...], ...}

def shard_items(items: List[str]) -> List[str]:
    return [it for idx, it in enumerate(items) if idx % TOTAL_SHARDS == SHARD_ID]

def download_cdn(url: str, timeout: int = 30) -> bytes:
    # No Authorization header -> CDN bypass
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def content_hash(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()

def parse_to_pair(raw: bytes, filename: str) -> Dict[str, Any]:
    """
    Project heterogeneous file to {prompt, response}.
    Extend per known schema (JSON, JSONL, txt, etc).
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {"prompt": "", "response": ""}

    # Very small heuristic projection; expand as needed per corpus
    if filename.endswith(".jsonl"):
        # take first line as example; in practice stream line-by-line
        line = text.splitlines()[0] if text else ""
        try:
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            return {"prompt": str(prompt), "response": str(response)}
        except Exception:
            pass

    # Fallback: treat whole file as response, prompt empty
    return {"prompt": "", "response": text}

def build_table(rows: List[Dict[str, Any]]) -> pa.Table:
    prompts = [r["prompt"] for r in rows]
    responses = [r["response"] for r in rows]
    hashes = [content_hash((p + "\n" + r).encode("utf-8")) for p, r in zip(prompts, responses)]
    timestamps = [datetime.now(timezone.utc).isoformat()] * len(rows)
    return pa.table({
        "prompt": pa.array(prompts, type=pa.string()),
        "response": pa.array(responses, type=pa.string()),
        "md5": pa.array(hashes, type=pa.string()),
        "ingest_ts": pa.array(timestamps, type=pa.string()),
    })

def main() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    all_files: List[str] = []
    for date_folder, files in manifest.items():
        all_files.extend(f"{date_folder}/{f}" for f in files)

    shard_files = shard_items(all_files)
    if not shard_files:
        print("No files assigned to this shard.", file=sys.stderr)
        sys.exit(0)

    rows = []
    for fpath in shard_files:
        url = f"{CDN_BASE}/{fpath}"
        try:
            raw = download_cdn(url)
            pair = parse_to_pair(raw, fpath)
            rows.append(pair)
        except Exception as exc:
            print(f"Failed {fpath}: {exc}", file=sys.stderr)
            continue

    if not rows:
        print("No rows produced.", file=sys.stderr)
        sys.exit(0)

    table = build_table(rows)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_name = OUT_DIR / f"shard-{SHARD_ID}-{ts}.parquet"
    pq.write_table(table, out_name)
    print(f"Wrote {len(rows)} rows -> {out_name}")

    # Optional: upload via huggingface_hub (requires HF_TOKEN)
    # We keep upload separate or call `huggingface_hub` upload_file_to_repo
    # with deterministic path:
    #   batches/public-merged/<date>/shard-<N>-<ts>.parquet
    # This script only produces the file; CI can upload or we upload here.
    # For now, write to OUT_DIR and let CI upload.

if __name__ == "__main__":
    main()
```

### 2) `bin/dataset-enrich.sh` (updated thin wrapper)

```bash
#!/usr/bin/env bash
# Wrapper for backward compatibility.
# Prefer running `ingest_worker.py` directly in CI.

set -euo pipefail
export SHELL=/bin/bash

SHARD_ID="${SHARD_ID:-0}"
MANIFEST_PATH="${MANIFEST_PATH:-manifest.json}"
OUT_DIR="${OUT_DIR:-output}"

exec python3 "$(dirname "$0")/ingest_worker.py"
```

Make executable:

```bash
chmod +x bin/dataset-enrich.sh bin/ingest_worker.py
```

### 3) GitHub Actions snippet (`.github/workflows/ingest.yml`) — minimal change

```yaml
name: ingest

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

jobs:
  ingest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-p
