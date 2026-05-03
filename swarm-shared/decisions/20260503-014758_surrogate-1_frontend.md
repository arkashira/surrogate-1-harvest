# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date and committed to the repo (or passed via `MANIFEST_JSON` env).  
- During the GitHub Actions run, each shard loads the manifest and **only downloads files assigned to its `SHARD_ID` via CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero HF API calls during streaming.  
- Projects heterogeneous files to `{prompt,response}` at parse time, writes normalized JSONL to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.  
- Keeps `lib/dedup.py` as the central md5 store (SQLite) for cross-run dedup.  
- Adds a small `bin/gen-manifest.py` for local/mac orchestration to produce the manifest (run once per date, commit or inject).  

This satisfies the HF rate-limit/CDN-bypass pattern and keeps Mac as orchestration-only while training/ingest run remotely.

---

## Files to change

- `bin/dataset-enrich.sh` → replace with `bin/dataset-enrich.py`
- Add `bin/gen-manifest.py`
- Update `.github/workflows/ingest.yml` to pass manifest (inline JSON or artifact) and use python worker
- Keep `lib/dedup.py` unchanged (dedup store)
- Update `requirements.txt` if needed (requests)

---

## Code snippets

### bin/gen-manifest.py
```python
#!/usr/bin/env python3
"""
Generate a repo-tree manifest for a date folder.
Usage:
  HF_TOKEN=... python bin/gen-manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json
"""
import argparse
import json
import os
import sys

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder under datasets (e.g. 2026-05-03)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)

    # List top-level folder once (non-recursive) to avoid 429 on big repos.
    # We expect date folder to contain dataset files (parquet/jsonl/etc).
    prefix = f"{args.date}/"
    try:
        tree = api.list_repo_tree(
            repo_id=args.repo,
            path=args.date,
            recursive=False,
        )
    except Exception as e:
        print(f"Failed to list repo tree for {args.date}: {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        # CDN download path (no auth, bypasses API rate limits)
        cdn_url = f"https://huggingface.co/datasets/{args.repo}/resolve/main/{prefix}{entry.path}"
        files.append({
            "path": f"{prefix}{entry.path}",
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
        })

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_by": "gen-manifest",
        "files": files,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### bin/dataset-enrich.py
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass enrichment worker.

Environment:
  SHARD_ID (0-15)        — shard index
  SHARD_TOTAL (default 16)
  MANIFEST_JSON          — inline JSON string or path to manifest file
  HF_TOKEN               — optional (only needed for upload)
  DATE_STR               — e.g. 2026-05-03
  OUT_DIR                — optional output root (default: batches/public-merged)

Behavior:
  - Load manifest (JSON string or file)
  - Assign files to shards by deterministic hash(slug) % SHARD_TOTAL
  - Download assigned files via CDN (no Authorization header)
  - Parse heterogeneous schemas -> {prompt, response}
  - Dedup via lib.dedup (central md5 store)
  - Write shard-N-<ts>.jsonl
"""
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

# local
from lib.dedup import DedupStore

SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", 16))
SHARD_ID = int(os.environ.get("SHARD_ID", 0))
HF_TOKEN = os.environ.get("HF_TOKEN", "")
DATE_STR = os.environ.get("DATE_STR", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "batches/public-merged"))
MANIFEST_SRC = os.environ.get("MANIFEST_JSON", "")

def _load_manifest() -> dict:
    if not MANIFEST_SRC:
        print("MANIFEST_JSON is required", file=sys.stderr)
        sys.exit(1)
    # If it looks like JSON inline, parse it; else treat as file path.
    src = MANIFEST_SRC.strip()
    if src.startswith("{") and src.endswith("}"):
        return json.loads(src)
    with open(src, "r", encoding="utf-8") as f:
        return json.load(f)

def _assign_to_shard(file_path: str, shard_total: int) -> int:
    # Deterministic shard by slug hash
    slug = Path(file_path).stem
    h = hashlib.md5(slug.encode()).hexdigest()
    return int(h, 16) % shard_total

def _safe_parquet_to_rows(path: Path):
    """Yield rows as dicts with {prompt, response} from parquet files."""
    try:
        table = pq.read_table(path, columns=None)
    except Exception as exc:
        print(f"Failed to read parquet {path}: {exc}", file=sys.stderr)
        return

    cols = set(table.column_names)

    # Try common patterns
    prompt_col = None
    response_col = None
    for c in cols:
        cl = c.lower()
        if "prompt" in cl:
            prompt_col = c
        if cl in ("response", "completion", "answer"):
            response_col = c

    # Fallback: first text col for prompt, second for response
    text_cols = [c for c in cols if pa.types.is_string(table.schema.field(c).type) or pa.types.is_large_string(table.schema.field(c).type)]
    if prompt_col is None and text_cols:
        prompt_col = text_cols[0]
    if response_col is None and len(text_cols) > 1:
        response_col = text_cols[1]

    if prompt_col is None or response_col is None:
        # Last resort: include all columns as JSON in prompt/response
        for batch in table.to_batches():
            for i in range(batch.num_rows):
                row = {k: batch[k][i].as_py() for k in batch.schema.names}
                yield {"prompt": json.dumps(row, ensure_ascii=False), "response": ""}
        return

    for batch in table.to_batches():
        prompts = batch[prompt_col].to_pylist()
        responses = batch[response_col].to_pylist()
        for p, r in zip(prompts, responses):
            yield {"prompt": p if isinstance(p, str) else str(p), "response": r if isinstance(r, str) else str(r)}

def _safe_jsonlines_to_rows(path: Path):
    with path.open("r", encoding="utf-8")
