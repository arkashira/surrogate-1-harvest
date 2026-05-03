# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix, plus optional `DATE` (defaults to today) and `HF_TOKEN`.
- Uses a **single API call** from the runner (after rate-limit window) to list one date folder via `list_repo_tree(path, recursive=False)` and saves the file list to `manifest-{DATE}.json`.
- Embeds the manifest in the worker so **Lightning training does CDN-only fetches with zero API calls** during data load.
- Downloads public dataset files via **HF CDN bypass** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — no Authorization header, avoids `/api/` rate limits.
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids `pyarrow.CastError`).
- Deduplicates via central md5 store (`lib/dedup.py`) and writes to:
  ```
  batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
  ```
- Commits use deterministic filenames (shard + timestamp) to avoid collisions across shards/iterations.
- Reuses existing HF token permissions; no state kept across runs (dedup cache lives on HF Space).

---

## `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID        - worker index (0..SHARD_TOTAL-1)
  SHARD_TOTAL     - total shards (default 16)
  DATE            - date folder on dataset repo (default today UTC)
  HF_TOKEN        - HuggingFace write token
  REPO            - dataset repo (default axentx/surrogate-1-training-pairs)
  MANIFEST_PATH   - where to save/load manifest (default manifest-{DATE}.json)
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
import concurrent.futures
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

# ---- config ----
REPO = os.getenv("REPO", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", datetime.datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
MANIFEST_PATH = os.getenv("MANIFEST_PATH", f"manifest-{DATE}.json")
API_RATE_LIMIT_RETRY = 360  # seconds after 429

if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

if not (0 <= SHARD_ID < SHARD_TOTAL):
    print(f"ERROR: SHARD_ID must be in [0, {SHARD_TOTAL - 1}]", file=sys.stderr)
    sys.exit(1)

# ---- paths ----
BASE_URL = f"https://huggingface.co/datasets/{REPO}/resolve/main"
DATE_PREFIX = f"batches/public-merged/{DATE}"
OUT_DIR = Path(DATE_PREFIX)
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ---- dedup ----
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

dedup = DedupStore()

# ---- hf api ----
api = HfApi(token=HF_TOKEN)

def list_date_folder() -> List[str]:
    """List files in DATE folder (non-recursive) via HF API."""
    try:
        tree = api.list_repo_tree(repo_id=REPO, path=DATE_PREFIX, recursive=False)
    except Exception as exc:
        # If 429, wait and retry once
        import time
        print(f"HF API error (possibly 429): {exc}. Waiting {API_RATE_LIMIT_RETRY}s...", file=sys.stderr)
        time.sleep(API_RATE_LIMIT_RETRY)
        tree = api.list_repo_tree(repo_id=REPO, path=DATE_PREFIX, recursive=False)
    # Expect tree entries with 'path'
    paths = [entry.path for entry in tree if hasattr(entry, "path")]
    return sorted(paths)

def save_manifest(paths: List[str]) -> None:
    manifest = {
        "repo": REPO,
        "date": DATE,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "paths": paths,
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest saved to {MANIFEST_PATH} ({len(paths)} paths)")

def load_manifest() -> List[str]:
    if not os.path.exists(MANIFEST_PATH):
        return []
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return manifest.get("paths", [])

# ---- schema projection ----
def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schema to {prompt, response}.
    Heuristic: look for common field names.
    """
    prompt = None
    response = None

    for key in ("prompt", "instruction", "input", "question", "query"):
        if key in raw and isinstance(raw[key], str) and raw[key].strip():
            prompt = raw[key].strip()
            break
    for key in ("response", "completion", "output", "answer"):
        if key in raw and isinstance(raw[key], str) and raw[key].strip():
            response = raw[key].strip()
            break

    # Fallback: if only one text-like field exists, split by separator
    if prompt is None or response is None:
        text_keys = [k for k in raw if isinstance(raw[k], str) and raw[k].strip()]
        if len(text_keys) == 1:
            text = raw[text_keys[0]].strip()
            sep_candidates = ["\n\n", "\n", "Answer:", "Response:", "###"]
            for sep in sep_candidates:
                if sep in text:
                    parts = text.split(sep, 1)
                    if len(parts) == 2:
                        prompt = parts[0].strip()
                        response = parts[1].strip()
                        break
            if prompt is None:
                prompt = ""
                response = text

    return {
        "prompt": prompt or "",
        "response": response or "",
    }

# ---- cdn download ----
def download_cdn(path: str) -> bytes:
    url = f"{BASE_URL}/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_file(content: bytes, path: str) -> List[Dict[str, str]]:
    """Parse parquet/jsonl content and project to pairs."""
    import io
    import pyarrow.parquet as pq
    import pyarrow as pa

    pairs = []
    if path.endswith(".parquet"):
        try:
            table = pq.read_table(io.BytesIO(content))
        except pa.ArrowInvalid:
            # fallback: try to read as generic bytes and skip
            return pairs
        for batch in table.to_batches(max_chunksize=1000):
            for row in batch.to_pylist():
                pairs.append(project_to_pair(row))
    elif path.endswith(".jsonl"):
        for line in content.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pairs.append(project_to_pair
