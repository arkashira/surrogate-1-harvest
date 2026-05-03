# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`s.

### What we’ll ship
- `bin/worker.py` — single, deterministic worker that:
  - Reads a pre-computed `manifest.json` (date folder → file slugs) produced once on the Mac orchestrator.
  - Downloads only its 1/16 shard via **CDN direct URLs** (no `datasets`/`list_repo_files` during training).
  - Projects each file to `{prompt, response}` at parse time (avoids mixed-schema `CastError`).
  - Deduplicates via centralized `lib/dedup.py` md5 store.
  - Writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns.
- `bin/gen-manifest.py` — run once on Mac after rate-limit window clears; does a single `list_repo_tree` per date folder and emits `manifest.json`.
- Update `bin/dataset-enrich.sh` to delegate to `python bin/worker.py` (keeps cron/workflow unchanged).
- Add `requirements.txt` extras if needed (`requests`, `pyarrow`, `tqdm`).

### Why this is highest value
- Eliminates HF API rate limits during training (CDN bypass).
- Prevents `pyarrow.CastError` from heterogeneous repo files.
- Keeps GitHub Actions parallelism (16 shards) but makes each shard robust and observable.
- Fits <2h: small, focused Python script + one-line shell wrapper change.

---

## Code Snippets

### `bin/gen-manifest.py`
Run once on Mac (or in workflow before matrix starts) to produce `manifest.json`.

```python
#!/usr/bin/env python3
"""
Generate manifest.json for a given date folder.
Usage:
  HF_TOKEN=... python bin/gen-manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="Folder under datasets/ to list")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)

    # List only top-level files in the date folder (non-recursive)
    folder = f"datasets/{args.date}"
    try:
        items = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
    except Exception as e:
        print(f"Failed to list {folder}: {e}", file=sys.stderr)
        sys.exit(1)

    files = [it.rfilename for it in items if it.type == "file"]
    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": sorted(files),
    }

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### `bin/worker.py`
Deterministic shard worker. Invoked by `dataset-enrich.sh` with `SHARD_ID` and `TOTAL_SHARDS`.

```python
#!/usr/bin/env python3
"""
CDN-bypass worker for surrogate-1 ingestion.

Environment:
  SHARD_ID (int, 0..TOTAL_SHARDS-1)
  TOTAL_SHARDS (int, default 16)
  HF_TOKEN (write token for axentx/surrogate-1-training-pairs)
  MANIFEST (path to manifest.json; default manifest.json)
  DATE (optional; overrides manifest date)
"""
import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
OUTPUT_REPO = "axentx/surrogate-1-training-pairs"

def hf_get(path: str, token: str, retries: int = 3, backoff: int = 5) -> bytes:
    """Download via CDN (no Authorization header required for public files)."""
    url = CDN_TEMPLATE.format(repo=OUTPUT_REPO, path=path)
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == retries:
                raise
            wait = backoff * attempt
            print(f"Download failed ({exc}), retry {attempt}/{retries} in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")

def project_to_pair(raw_bytes: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response} only.
    Supports:
      - JSONL with {prompt, response}
      - Parquet with any schema (select prompt/response cols if present, else first two str cols)
    """
    suffix = Path(filename).suffix.lower()
    pairs = []

    try:
        if suffix == ".parquet":
            table = pq.read_table(pa.BufferReader(raw_bytes))
            # Find candidate columns
            prompt_col = None
            response_col = None
            for col in table.column_names:
                lc = col.lower()
                if "prompt" in lc:
                    prompt_col = col
                elif "response" in lc or "completion" in lc or "answer" in lc:
                    response_col = col

            if prompt_col is None or response_col is None:
                # Fallback: first two string columns
                str_cols = [c for c in table.column_names if pa.types.is_string(table.schema.field(c).type)]
                if len(str_cols) >= 2:
                    prompt_col, response_col = str_cols[0], str_cols[1]
                else:
                    raise ValueError(f"Cannot find prompt/response in {filename}")

            prompts = table.column(prompt_col).to_pylist()
            responses = table.column(response_col).to_pylist()
            for p, r in zip(prompts, responses):
                if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                    pairs.append({"prompt": p.strip(), "response": r.strip()})
            return pairs

        # Assume JSONL
        for line in raw_bytes.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            prompt = str(obj.get("prompt") or obj.get("input") or "").strip()
            response = str(obj.get("response") or obj.get("output") or obj.get("completion") or "").strip()
            if prompt and response:
                pairs.append({"prompt": prompt, "response": response})
        return pairs

    except Exception as exc:
        print(f"Failed to project {filename}: {exc}", file=sys.stderr)
        return []

def content_md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()

def build_output_path(date_folder: str, shard_id: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    return f"batches/public-merged/{date_folder}/shard{shard_id}-{ts}.jsonl"

def upload_output(content: str, token: str) -> None:
    """
    Upload via HF API (counted against commit cap).
    Keep commits small and deterministic per shard to avoid collisions.
    """
    from huggingface_hub import upload_file

    path_in_repo = build_output_path("TBD", 0)  # placeholder; will be replaced by caller logic
    # We'll implement upload in main() after path is finalized.

