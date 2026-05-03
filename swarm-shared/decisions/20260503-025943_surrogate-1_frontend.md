# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic shard assignment by filename hash
- Saves file manifest JSON for reproducibility
- Downloads assigned files via **HF CDN bypass** (`resolve/main/...` URLs, no Authorization header)
- Projects heterogeneous schemas to `{prompt,response}` only at parse time
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Updates GitHub Actions matrix to pass `DATE` and use Python runner

---

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage:
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py

Environment:
  HF_TOKEN          - HuggingFace write token
  SHARD_ID          - 0..15
  SHARD_TOTAL       - default 16
  DATE              - folder/date to ingest (e.g. 2026-04-29)
  DATASET_REPO      - default axentx/surrogate-1-training-pairs
  BATCH_SIZE        - lines per output file (default 50_000)
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

import requests
from huggingface_hub import HfApi, hf_hub_download

# ---- config ----
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50000"))

if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

# ---- paths ----
BASE_DIR = Path(__file__).parent.parent
LIB_DIR = BASE_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from dedup import DedupStore  # type: ignore

OUT_DIR = BASE_DIR / "batches" / "public-merged" / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- helpers ----
def slug_hash(slug: str) -> int:
    """Deterministic 0..2^32-1 hash for shard assignment."""
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def assign_shard(slug: str, total: int) -> int:
    return slug_hash(slug) % total

def hf_api() -> HfApi:
    return HfApi(token=HF_TOKEN)

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: list top-level files in date folder (non-recursive)."""
    api = hf_api()
    # list_repo_tree with recursive=False to avoid pagination explosion
    tree = api.list_repo_tree(
        repo_id=DATASET_REPO,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    return files

def cdn_download_url(path: str) -> str:
    """CDN bypass URL (no auth header required)."""
    return f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main/{path}"

def safe_download(url: str, dest: Path, max_retries: int = 3, backoff: int = 360) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 429:
                wait = backoff if attempt == 1 else backoff * attempt
                print(f"Rate-limited 429, waiting {wait}s (attempt {attempt})", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return True
        except Exception as exc:
            print(f"Download failed (attempt {attempt}): {exc}", file=sys.stderr)
            if attempt == max_retries:
                return False
            time.sleep(5 * attempt)
    return False

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Heuristic: look for common field names; fallback to first/last text fields.
    """
    # Common field names seen in public datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "user", "query"}
    response_keys = {"response", "output", "answer", "assistant", "completion", "result"}

    prompt = None
    response = None

    for k in prompt_keys:
        if k in raw and isinstance(raw[k], str) and raw[k].strip():
            prompt = raw[k].strip()
            break
    for k in response_keys:
        if k in raw and isinstance(raw[k], str) and raw[k].strip():
            response = raw[k].strip()
            break

    if prompt is None or response is None:
        # fallback: pick first and last text-ish fields
        text_fields = [v for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(text_fields) >= 2:
            prompt, response = text_fields[0].strip(), text_fields[-1].strip()
        elif len(text_fields) == 1:
            prompt, response = text_fields[0].strip(), ""
        else:
            prompt, response = json.dumps(raw), ""

    return {"prompt": prompt, "response": response}

# ---- main ----
def main() -> None:
    print(f"Starting shard {SHARD_ID}/{SHARD_TOTAL} for date={DATE}", flush=True)

    # 1) list files once
    files = list_date_files(DATE)
    print(f"Found {len(files)} files in {DATE}/", flush=True)

    # save manifest for reproducibility
    manifest_path = OUT_DIR / f"manifest-shard{SHARD_ID}.json"
    manifest = {
        "date": DATE,
        "shard_id": SHARD_ID,
        "shard_total": SHARD_TOTAL,
        "files": files,
        "assigned_files": [],
        "ts_utc": datetime.now(timezone.utc).isoformat(),
    }

    # 2) assign shard
    assigned = [f for f in files if assign_shard(f, SHARD_TOTAL) == SHARD_ID]
    manifest["assigned_files"] = assigned
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Assigned {len(assigned)} files to shard {SHARD_ID}", flush=True)

    # 3) init dedup
    dedup = DedupStore()

    # 4) process files
    batch: List[Dict[str, str]] = []
    batch_idx = 0
    total_pairs = 0
    skipped_dupes = 0

    def flush_batch() -> None:
        nonlocal batch_idx, batch
        if not batch:
            return
        ts = datetime.utcnow().strftime("%H%M%S")
        out_file = OUT_DIR / f"shard{SHARD_ID}-{ts}-{batch_idx:04d}.jsonl"
        out_file.write_text("\n".join(json.dumps(x) for x in batch) + "\n")
        print(f"Wrote {len(batch)} pairs to {out_file.name}", flush=True)
        batch_idx += 1
        batch = []

    for file in assigned:
        print(f"Processing {file}...", flush=True)
        local_path = OUT_DIR / f".tmp-{hashlib.md5(file.encode
