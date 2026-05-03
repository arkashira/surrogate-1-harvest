# surrogate-1 / quality

## Implementation Plan — CDN-Bypass Manifest-Driven Ingestion

**Scope**: Replace `bin/dataset-enrich.sh` with a manifest-driven, CDN-bypass ingestion worker that:
- Eliminates HF API rate limits (429) during data fetch
- Avoids mixed-schema `pyarrow` errors by per-file selective parsing
- Uses deterministic shard assignment and dedup via central SQLite
- Outputs `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`

**Why this is highest value**:
- Directly fixes the HF API 429 and pyarrow CastError patterns from the knowledge base
- Enables stable, high-throughput ingestion without quota exhaustion
- Minimal code (~200 LOC) and fits <2h implementation + test

---

### 1. File layout changes

```
bin/
  dataset-enrich.sh          # replaced by Python worker
  worker.py                  # new: manifest-driven CDN ingest
  gen_manifest.py            # new: one-time Mac-side manifest generator
lib/
  dedup.py                   # unchanged (central md5 store)
```

---

### 2. Step-by-step implementation

#### A. Generate manifest on Mac (once per date folder)

`bin/gen_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate file manifest for a date folder.
Run on Mac (or any machine with HF token) to avoid API calls during training.

Usage:
  HF_TOKEN=hf_xxx python bin/gen_manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json
"""
import argparse
import json
import os
from huggingface_hub import HfApi, list_repo_tree

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="Folder under datasets/ to list")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    # List only top-level in the date folder (non-recursive to avoid pagination explosion)
    tree = list_repo_tree(
        repo_id=args.repo,
        path=args.date,
        recursive=False,
        repo_type="dataset"
    )

    files = []
    for item in tree:
        if item.type != "file":
            continue
        # Only include files we expect (parquet/jsonl/etc)
        if not item.path.endswith((".parquet", ".jsonl", ".json")):
            continue
        files.append({
            "path": item.path,          # relative to repo root
            "size": getattr(item, "size", None)
        })

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": files
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/gen_manifest.py
```

---

#### B. New worker: `bin/worker.py`

This is the replacement for `dataset-enrich.sh`. It:
- Reads a manifest JSON
- Downloads each file via **CDN URL** (no Authorization header → bypasses API rate limit)
- Projects to `{prompt, response}` per schema rules
- Deduplicates via `lib/dedup.py`
- Writes `shard-N-<ts>.jsonl` to `batches/public-merged/<date>/`

`bin/worker.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 \
  python bin/worker.py \
    --manifest manifest-2026-05-03.json \
    --out-dir batches/public-merged
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore

HF_CDN = "https://huggingface.co/datasets"

def deterministic_shard(key: str, total: int) -> int:
    """Map key to shard by md5."""
    digest = hashlib.md5(key.encode()).hexdigest()
    return int(digest, 16) % total

def cdn_url(repo: str, path: str) -> str:
    return f"{HF_CDN}/{repo}/resolve/main/{path}"

def safe_fetch(url: str, max_retries: int = 3, backoff: int = 360) -> Optional[bytes]:
    """Download with retry; respects HF CDN rate limits."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 429:
                wait = backoff if attempt == 0 else backoff * (2 ** attempt)
                print(f"CDN 429, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == max_retries - 1:
                print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
                return None
            time.sleep(5)
    return None

def project_record(raw: Dict[str, Any], source_path: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Extend this mapping per observed schema.
    """
    # Common patterns observed in surrogate-1 datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    # Normalize keys to lowercase for matching
    low = {k.lower(): (k, v) for k, v in raw.items() if isinstance(k, str)}

    prompt = None
    response = None

    for pk in prompt_keys:
        if pk in low:
            prompt = str(low[pk][1])
            break
    for rk in response_keys:
        if rk in low:
            response = str(low[rk][1])
            break

    # Fallback: if only one text-like field exists, split by separator
    if prompt is None or response is None:
        text_fields = [v for _, v in low.values() if isinstance(v, str) and len(v) > 20]
        if len(text_fields) == 1:
            parts = text_fields[0].split("\n\n", 1)
            if len(parts) == 2:
                prompt, response = parts[0], parts[1]

    if not prompt or not response:
        return None

    return {"prompt": prompt.strip(), "response": response.strip()}

def iter_parquet_projected(content: bytes, source_path: str) -> Iterable[Dict[str, str]]:
    """Stream parquet bytes and project rows."""
    try:
        table = pq.read_table(pa.BufferReader(content))
    except pa.ArrowInvalid as exc:
        print(f"Skipping invalid parquet {source_path}: {exc}", file=sys.stderr)
        return

    for batch in table.to_batches(max_chunksize=1000):
        cols = {name: batch.column(name) for name in batch.schema.names}
        # Build rows without materializing full pandas
        n = batch.num_rows
        for i in range(n):
            raw = {k: cols[k][i].as_py() for k in cols}
            proj = project_record(raw, source_path)
            if proj:
                yield proj

def iter_jsonl_projected(content: bytes, source_path: str) -> Iterable[Dict[str, str]]:
    text = content.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip
