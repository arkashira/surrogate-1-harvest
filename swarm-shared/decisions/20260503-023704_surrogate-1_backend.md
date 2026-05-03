# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Uses a **pre-listed manifest** (JSON) generated once per date to avoid recursive `list_repo_files` and HF API rate limits
- Downloads files via **HF CDN** (`resolve/main/...`) with zero Authorization headers (bypasses `/api/` 429 limits)
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids PyArrow CastError)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Returns exit code 0 on success, non-zero on fatal failure (so GitHub Actions matrix fails fast)

### Steps (est. 90 min)
1. Create `bin/dataset-enrich.py` (main worker) — 45 min
2. Create `bin/gen-manifest.py` (one-time manifest generator for the date folder) — 15 min
3. Update `bin/dataset-enrich.sh` to delegate to Python (or replace it) — 10 min
4. Update `.github/workflows/ingest.yml` to generate manifest once and pass to each shard — 10 min
5. Quick smoke test (local + dry-run) — 10 min

---

## Code Snippets

### 1. `bin/gen-manifest.py` (run once per date from Mac)
```python
#!/usr/bin/env python3
"""
Generate a flat manifest for a date folder to avoid recursive HF API calls.
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
from huggingface_hub import HfApi, list_repo_tree

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under public-merged/ or raw/")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--prefix", default="public-raw", help="Top-level folder in repo")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token) if token else HfApi()

    # List only one level at a time to avoid huge recursive pagination.
    # We assume date folder contains only files (or shallow subfolders).
    base = f"{args.prefix}/{args.date}"
    entries = list_repo_tree(repo_id=args.repo, path=base, recursive=False)

    files = []
    for e in entries:
        if e.type == "file":
            files.append(e.path)
        elif e.type == "dir":
            # shallow list inside subfolder (avoid deep recursion)
            sub = list_repo_tree(repo_id=args.repo, path=e.path, recursive=False)
            for se in sub:
                if se.type == "file":
                    files.append(se.path)

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "prefix": args.prefix,
        "files": sorted(files),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

### 2. `bin/dataset-enrich.py` (new worker)
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1.

Environment:
  SHARD_ID (int, 0..SHARD_TOTAL-1)
  SHARD_TOTAL (int, default 16)
  DATE (YYYY-MM-DD)
  HF_TOKEN (optional for upload; reads can be anonymous via CDN)
  REPO_ID (default axentx/surrogate-1-training-pairs)
  MANIFEST_PATH (optional; if not provided, uses manifest in repo at
                 public-raw/{DATE}/manifest.json or similar)

Behavior:
  - Reads manifest (list of files for DATE)
  - Takes deterministic shard by hashing file path (or index) modulo SHARD_TOTAL
  - Downloads assigned files via HF CDN (no auth header)
  - Projects each file to {prompt, response} (schema-agnostic)
  - Deduplicates by md5 via lib/dedup.py
  - Writes shard output to batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
  - Uploads output file to REPO_ID via HF API (uses HF_TOKEN)
"""
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download, upload_file

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

CDN_BASE = "https://huggingface.co/datasets"

def get_env(key: str, default: Optional[str] = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        raise ValueError(f"Missing environment variable: {key}")
    return val

def deterministic_shard(path: str, shard_total: int, shard_id: int) -> bool:
    h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    return (h % shard_total) == shard_id

def download_via_cdn(repo: str, file_path: str, local_path: Path) -> None:
    url = f"{CDN_BASE}/{repo}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    local_path.write_bytes(resp.content)

def project_to_pair(obj: Dict[str, Any], file_path: str) -> Optional[Dict[str, str]]:
    """
    Schema-agnostic projection to {prompt, response}.
    Supports common patterns seen in surrogate-1 raw files.
    """
    # If already correct shape
    if "prompt" in obj and "response" in obj:
        return {"prompt": str(obj["prompt"]), "response": str(obj["response"])}

    # Common aliases
    prompt_keys = {"prompt", "input", "question", "instruction", "text"}
    response_keys = {"response", "output", "answer", "completion", "target"}

    prompt = None
    response = None
    for k in obj:
        if k in prompt_keys and prompt is None:
            prompt = str(obj[k])
        elif k in response_keys and response is None:
            response = str(obj[k])

    if prompt is not None and response is not None:
        return {"prompt": prompt, "response": response}

    # Fallback: if exactly two string fields, treat as pair
    str_fields = {k: v for k, v in obj.items() if isinstance(v, str)}
    if len(str_fields) == 2:
        vals = list(str_fields.values())
        return {"prompt": vals[0], "response": vals[1]}

    # Last resort: encode whole object as prompt, empty response (will be filtered later)
    # Log warning via stderr for visibility in CI
    print(f"WARN: cannot project {file_path}:{json.dumps(obj)[:200]}", file=sys.stderr)
    return None

def process_file(
    repo: str,
    file_path: str,
    dedup: DedupStore,
    tmp_dir: Path,
) -> List[Dict[str, str]]:
    local_file = tmp_dir / uuid.uuid4().hex
    try:
        download_via_cdn(repo, file_path, local_file)
        content = local_file.read
