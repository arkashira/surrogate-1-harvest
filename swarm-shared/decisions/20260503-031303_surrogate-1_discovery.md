# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree(path, recursive=False)` per date folder → deterministic file list saved to `manifest-{DATE}.json`
- Shards files by `hash(slug) % SHARD_TOTAL` → each runner processes only its slice
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header → avoids 429 API limits during data load
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with slug-derived attribution in filename only (no extra columns)
- Reuses existing HF dataset repo; spreads writes across shards to respect 128/hr commit cap
- Exits 0 on success, non-zero on fatal error (GitHub Actions will retry)

---

## Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Env:
  SHARD_ID      (int, 0..15)
  SHARD_TOTAL   (int, default 16)
  DATE          (YYYY-MM-DD, required)
  HF_TOKEN      (write token for axentx/surrogate-1-training-pairs)
  REPO          (default "axentx/surrogate-1-training-pairs")
  MANIFEST_DIR  (default ".")
"""

import os
import sys
import json
import hashlib
import datetime
import pathlib
import time
import requests
from typing import List, Dict, Any

try:
    from huggingface_hub import HfApi, hf_hub_download
except ImportError:
    print("ERROR: huggingface_hub not installed", file=sys.stderr)
    sys.exit(1)

# ── config ──────────────────────────────────────────────────────────────
SHARD_ID = int(os.getenv("SHARD_ID", -1))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", 16))
DATE = os.getenv("DATE", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
REPO = os.getenv("REPO", "axentx/surrogate-1-training-pairs")
MANIFEST_DIR = os.getenv("MANIFEST_DIR", ".")

if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
    print(f"ERROR: SHARD_ID must be 0..{SHARD_TOTAL-1}", file=sys.stderr)
    sys.exit(1)

if not DATE:
    print("ERROR: DATE (YYYY-MM-DD) is required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
SESSION = requests.Session()

# ── helpers ─────────────────────────────────────────────────────────────
def slug_hash_bucket(slug: str, n: int) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % n

def list_date_files(date: str) -> List[str]:
    """
    Single API call: list top-level files under date folder.
    Returns relative paths like "2026-04-29/file1.jsonl"
    """
    # If manifest exists, reuse it (idempotent across reruns)
    manifest_path = pathlib.Path(MANIFEST_DIR) / f"manifest-{date}.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)

    items = []
    try:
        tree = API.list_repo_tree(
            repo_id=REPO,
            path=date,
            recursive=False,
        )
    except Exception as e:
        print(f"ERROR: list_repo_tree failed: {e}", file=sys.stderr)
        sys.exit(1)

    for item in tree:
        if item.get("type") == "file":
            items.append(f"{date}/{item['path']}")

    # Save manifest for reuse within this run / debugging
    with open(manifest_path, "w") as f:
        json.dump(items, f)
    return items

def cdn_download(repo: str, path: str) -> bytes:
    """
    Download via HF CDN (no auth header) to bypass API rate limits.
    """
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

def parse_to_pair(raw: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous file schemas to {prompt, response}.
    Supports: .jsonl, .json, .parquet (via hf_hub_download fallback).
    """
    name = pathlib.Path(filename).name.lower()
    pairs = []

    # .jsonl lines
    if name.endswith(".jsonl"):
        for line in raw.decode().strip().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            if prompt and response:
                pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    # single .json object or list
    if name.endswith(".json"):
        try:
            obj = json.loads(raw.decode())
        except Exception:
            return []
        if isinstance(obj, list):
            for item in obj:
                prompt = item.get("prompt") or item.get("input") or item.get("question") or ""
                response = item.get("response") or item.get("output") or item.get("answer") or ""
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
        elif isinstance(obj, dict):
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            if prompt and response:
                pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    # .parquet: use hf_hub_download + pyarrow (avoids streaming/mixed-schema issues)
    if name.endswith(".parquet"):
        try:
            import pyarrow.parquet as pq
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
                tmp.write(raw)
                tmp.flush()
                table = pq.read_table(tmp.name)
                df = table.to_pandas()
            cols = df.columns.tolist()
            prompt_col = next((c for c in ["prompt", "input", "question"] if c in cols), None)
            response_col = next((c for c in ["response", "output", "answer"] if c in cols), None)
            if prompt_col and response_col:
                for _, row in df.iterrows():
                    p = str(row[prompt_col]).strip()
                    r = str(row[response_col]).strip()
                    if p and r:
                        pairs.append({"prompt": p, "response": r})
            return pairs
        except Exception:
            return []

    return []

def load_dedup_store():
    # Import local dedup module
    sys.path.insert(0, str(pathlib.Path(__file__).parent / "lib"))
    try:
        from dedup import DedupStore
        return DedupStore()
    except Exception as e:
        print(f"WARN: dedup store unavailable: {e}", file=sys.stderr)
        return None

def upload_batch(date: str, shard_id: int, batch: List[Dict[str, str]]) -> None:
    if not batch:
        return

    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    filename = f"shard{shard_id}-{ts}.jsonl"
   
