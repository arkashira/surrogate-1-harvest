# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses a single `list_repo_tree` call (per `DATE`) to produce `manifest-<DATE>.json` (cached on Mac/CI)
- Assigns deterministic shard membership via `hash(slug) % SHARD_TOTAL`
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Deduplicates via central `lib/dedup.py` md5 store
- Emits `batches/public-merged/<DATE>/shard<SHARD_ID>-<HHMMSS>.jsonl` with **only** `{prompt, response}` (no `source`, no `ts`)
- Uses filename-based attribution (`batches/public-merged/<date>/<slug>.parquet` style) if upstream provenance is needed

---

## Concrete changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass, manifest-driven ingestion worker for surrogate-1.

Usage (CI/local):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
    HF_TOKEN=hf_xxx \
    python bin/dataset-enrich.py

Environment:
  SHARD_ID          - worker index [0..SHARD_TOTAL-1]
  SHARD_TOTAL       - total parallel workers (default 16)
  DATE              - date folder in dataset repo (e.g. 2026-05-03)
  HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
  MANIFEST_PATH     - optional local manifest JSON (if present, skips list_repo_tree)
  DATASET_REPO      - default: datasets/axentx/surrogate-1-training-pairs
  UPLOAD_BATCH_SIZE - number of records before flush (default 5000)
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# ---- config ----
DATASET_REPO = os.getenv("DATASET_REPO", "datasets/axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE")
MANIFEST_PATH = os.getenv("MANIFEST_PATH")
UPLOAD_BATCH_SIZE = int(os.getenv("UPLOAD_BATCH_SIZE", "5000"))

if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)
if not DATE:
    print("DATE required (YYYY-MM-DD)", file=sys.stderr)
    sys.exit(1)

# ---- paths ----
BASE_PATH = Path(__file__).parent.parent
LIB_PATH = BASE_PATH / "lib"
sys.path.insert(0, str(LIB_PATH))

try:
    from dedup import DedupStore
except ImportError as e:
    print(f"Failed to import dedup: {e}", file=sys.stderr)
    sys.exit(1)

# ---- hf api ----
api = HfApi(token=HF_TOKEN)

# ---- util ----
def deterministic_shard(slug: str, total: int) -> int:
    return int(hashlib.sha256(slug.encode("utf-8")).hexdigest(), 16) % total

def cdn_url(repo: str, path: str) -> str:
    """HF CDN bypass URL (no auth, no API rate-limit)."""
    # repo format: datasets/owner/name
    if repo.startswith("datasets/"):
        _, owner, name = repo.split("/", 2)
    else:
        owner, name = repo.split("/", 1)
    return f"https://huggingface.co/datasets/{owner}/{name}/resolve/main/{path}"

def load_manifest(date: str) -> List[str]:
    """Return list of file paths for date folder."""
    if MANIFEST_PATH and Path(MANIFEST_PATH).exists():
        with open(MANIFEST_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and "files" in data:
            return [f for f in data["files"] if f.startswith(f"{date}/")]
        return [f for f in data if f.startswith(f"{date}/")]

    # single API call: non-recursive tree for the date folder
    try:
        tree = list_repo_tree(
            repo_id=DATASET_REPO.replace("datasets/", ""),
            path=DATE,
            recursive=False,
            token=HF_TOKEN,
        )
    except Exception as e:
        print(f"list_repo_tree failed: {e}", file=sys.stderr)
        sys.exit(1)

    files = [item.path for item in tree if item.type == "file"]
    # cache manifest locally for reuse in same run
    manifest_out = Path("manifest") / f"manifest-{date}.json"
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_out, "w") as f:
        json.dump(files, f)
    return files

def parse_to_pair(raw: Any, path: str) -> Optional[Dict[str, str]]:
    """
    Project arbitrary file schema to {prompt, response}.
    Supports common patterns seen in surrogate-1 training pairs:
      - JSON/JSONL with 'prompt'/'response' or 'instruction'/'output'
      - Parquet row converted to dict
    Returns None if unparseable.
    """
    if isinstance(raw, dict):
        d = raw
    else:
        # fallback: try object as dict
        try:
            d = dict(raw)
        except Exception:
            return None

    prompt = d.get("prompt") or d.get("instruction") or d.get("input") or d.get("question")
    response = d.get("response") or d.get("output") or d.get("answer") or d.get("completion")

    if prompt is None or response is None:
        # last-resort: look for any string fields
        str_fields = [v for v in d.values() if isinstance(v, str) and len(v) > 10]
        if len(str_fields) >= 2:
            prompt, response = str_fields[0], str_fields[1]
        else:
            return None

    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

# ---- main ----
def main() -> None:
    dedup = DedupStore()
    files = load_manifest(DATE)

    my_files = [
        f for f in files
        if deterministic_shard(f, SHARD_TOTAL) == SHARD_ID
    ]

    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} assigned {len(my_files)} files", flush=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_name = f"shard{SHARD_ID}-{timestamp}.jsonl"
    out_dir = BASE_PATH / "batches" / "public-merged" / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / out_name

    records: List[Dict[str, str]] = []
    total = 0
    accepted = 0
    skipped_dup = 0
    failed_parse = 0

    for file_path in sorted(my_files):
        total += 1
        url = cdn_url(DATASET_REPO, file_path)
        slug = Path(file_path).stem  # crude attribution via filename

        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"Download failed {file_path}: {e}", flush=True)
            continue

        # Try parse as JSON lines first (common in surrogate-1)
        content = resp.content

