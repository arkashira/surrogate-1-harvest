# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses **single API call** from the runner (once per cron tick) to list one date folder via `list_repo_tree(path, recursive=False)` → saves to `manifest-{DATE}.json`
- Each shard deterministically hashes `slug` → picks assigned files (`hash(slug) % SHARD_TOTAL == SHARD_ID`)
- Downloads only assigned files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` 429 limits)
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids `pyarrow.CastError`)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with deterministic filename (no collisions)
- Exits 0 on success, non-zero on fatal error (GitHub Actions handles retries)

### Why this is the highest-value incremental improvement
- Directly applies the **HF CDN bypass** and **manifest pre-list** patterns from the lessons (eliminates 429s during training)
- Replaces brittle shell script with Python that handles schema heterogeneity and deterministic sharding cleanly
- Keeps within <2h: single file replacement, minimal refactor, reuses existing `lib/dedup.py` and workflow matrix

---

## Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GitHub Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx python bin/dataset-enrich.py

Environment:
  SHARD_ID          - shard index (0..SHARD_TOTAL-1)
  SHARD_TOTAL       - total shards (default 16)
  DATE              - date folder in dataset (YYYY-MM-DD, default today)
  HF_TOKEN          - HF write token (for upload + optional API list)
  MANIFEST_PATH     - optional pre-generated manifest JSON
  DATASET_REPO      - dataset repo (default axentx/surrogate-1-training-pairs)
  DRY_RUN           - if set, skip upload
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any

try:
    import requests
    from huggingface_hub import HfApi, hf_hub_download
except ImportError as e:
    print(f"Missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

# ---- config ----
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
MANIFEST_PATH = os.getenv("MANIFEST_PATH")
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
API = HfApi(token=HF_TOKEN) if HF_TOKEN else None

# ---- helpers ----
def deterministic_shard(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def list_date_folder_via_api(date_folder: str) -> List[str]:
    """Single API call: list files in one date folder (non-recursive)."""
    try:
        items = API.list_repo_tree(
            repo_id=DATASET_REPO,
            path=date_folder,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"API list_repo_tree failed: {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for item in items:
        if isinstance(item, dict):
            p = item.get("path")
            if p:
                files.append(p)
        else:
            files.append(str(item))
    return files

def build_manifest(date_folder: str) -> List[str]:
    """Return list of file paths under date_folder (parquet/jsonl)."""
    if MANIFEST_PATH and Path(MANIFEST_PATH).exists():
        with open(MANIFEST_PATH) as f:
            data = json.load(f)
            if isinstance(data, dict) and "files" in data:
                return data["files"]
            return data

    files = list_date_folder_via_api(date_folder)
    # Keep only data files
    files = [f for f in files if f.endswith((".parquet", ".jsonl", ".json"))]
    # Save for reuse in this run
    Path("manifest-latest.json").write_text(json.dumps({"date": date_folder, "files": files}, indent=2))
    return files

def cdn_download(url: str, dest: Path) -> Path:
    """Download via CDN (no auth) with retry/backoff."""
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
            return dest
        except Exception as e:
            wait = 2 ** attempt
            print(f"Download attempt {attempt+1} failed ({e}), retrying in {wait}s", file=sys.stderr)
            import time
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {url}")

def parse_file_to_pairs(local_path: Path) -> List[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Supports:
      - HF datasets-style dicts with 'prompt'/'response' keys (case-insensitive)
      - raw text lines treated as prompt with empty response
    """
    import pyarrow.parquet as pq
    import numpy as np

    pairs = []
    suffix = local_path.suffix.lower()

    try:
        if suffix == ".parquet":
            table = pq.read_table(local_path)
            cols = {c.lower(): c for c in table.column_names}
            prompt_col = cols.get("prompt") or cols.get("instruction") or cols.get("input")
            response_col = cols.get("response") or cols.get("output") or cols.get("completion")

            if prompt_col and response_col:
                prompts = table.column(prompt_col).to_pylist()
                responses = table.column(response_col).to_pylist()
                for p, r in zip(prompts, responses):
                    if p is not None:
                        pairs.append({"prompt": str(p), "response": str(r) if r is not None else ""})
            else:
                # fallback: serialize rows
                for row in table.to_pylist():
                    pairs.append({"prompt": json.dumps(row), "response": ""})

        elif suffix in (".jsonl", ".json"):
            text = local_path.read_text()
            if suffix == ".json":
                data = json.loads(text)
                items = data if isinstance(data, list) else [data]
            else:
                items = [json.loads(ln) for ln in text.strip().splitlines() if ln.strip()]

            for item in items:
                if isinstance(item, dict):
                    prompt = item.get("prompt") or item.get("instruction") or item.get("input") or json.dumps(item)
                    response = item.get("response") or item.get("output") or item.get("completion") or ""
                    pairs.append({"prompt": str(prompt), "response": str(response)})
                else:
                    pairs.append({"prompt": str(item), "response": ""})
        else:
            print(f"Unsupported file type {suffix}, skipping", file=sys.stderr)
    except Exception as e:
        print(f"Parse error {local_path}: {e}", file=sys.stderr)

    return pairs

def md5_of_pair(pair: Dict[str, str]) -> str:
    payload = json.dumps(pair, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(payload.encode()).hexdigest()

def upload_shard(output_path: str, lines: List[str]) -> None:
    if DRY_RUN:
