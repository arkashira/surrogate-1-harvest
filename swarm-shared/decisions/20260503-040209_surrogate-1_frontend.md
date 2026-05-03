# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed file manifest** (`manifest-{DATE_FOLDER}.json`) generated once per date to avoid recursive HF API calls and rate limits.
- Worker deterministically selects its 1/16 slice by `hash(slug) % SHARD_TOTAL == SHARD_ID`.
- Downloads selected files **via HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` rate limits).
- Projects each file to `{prompt, response}` only at parse time (avoids `load_dataset(streaming=True)` on mixed schemas and `pyarrow.CastError` from heterogeneous schemas).
- Deduplicates via central md5 store (`lib/dedup.py`) and writes to deterministic shard output:
  ```
  batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
  ```
- Reuses existing `requirements.txt` (`datasets`, `huggingface_hub`, `pyarrow`, `numpy`, `requests`).
- Adds `bin/fetch-manifest.py` (run once per date from Mac) to list one date folder via HF API and save JSON for workers.

---

### 1) New / updated files

#### `bin/fetch-manifest.py`
Run once per date (after rate-limit window clears) from Mac. Lists one date folder non-recursively and saves manifest.

```python
#!/usr/bin/env python3
"""
Fetch a flat file list for a single date folder from the HF dataset repo.
Usage:
  python bin/fetch-manifest.py 2026-05-03
"""
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "manifests")

def main():
    date_folder = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    api = HfApi()
    # Non-recursive: one API call, paginated safely (100/page)
    files = api.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    entries = []
    for f in files:
        if f.rfilename.endswith((".parquet", ".jsonl", ".json")):
            entries.append({
                "path": f.rfilename,
                "size": getattr(f, "size", None)
            })
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"manifest-{date_folder}.json")
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump({"date_folder": date_folder, "files": entries}, fp, indent=2)
    print(f"Wrote {len(entries)} entries to {out_path}")

if __name__ == "__main__":
    main()
```

#### `bin/dataset-enrich.py`
New worker script (replaces shell script). Deterministic shard assignment, CDN downloads, schema projection, dedup, upload.

```python
#!/usr/bin/env python3
"""
Shard worker for public-dataset ingest.

Environment:
  SHARD_ID (int, 0..15)
  SHARD_TOTAL (int, default 16)
  DATE_FOLDER (optional, e.g. 2026-05-03)
  HF_TOKEN (write token for axentx/surrogate-1-training-pairs)
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from datasets import load_dataset
from huggingface_hub import HfApi, hf_hub_download

# ── paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
LIB_DIR = REPO_ROOT / "lib"
MANIFESTS_DIR = REPO_ROOT / "manifests"
sys.path.insert(0, str(LIB_DIR))

try:
    from dedup import DedupStore
except ImportError as exc:
    print("ERROR: lib/dedup.py not found or invalid", file=sys.stderr)
    raise

# ── config ───────────────────────────────────────────────────────────────────
HF_DATASET_REPO = "datasets/axentx/surrogate-1-training-pairs"
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
if not (0 <= SHARD_ID < SHARD_TOTAL):
    print(f"ERROR: SHARD_ID must be in [0, {SHARD_TOTAL - 1}]", file=sys.stderr)
    sys.exit(1)

DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
MANIFEST_PATH = MANIFESTS_DIR / f"manifest-{DATE_FOLDER}.json"

# ── helpers ──────────────────────────────────────────────────────────────────
def row_hash(row: Dict[str, Any]) -> str:
    # Deterministic content hash for dedup (same as central store)
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()

def project_to_pair(obj: Any, source_path: str) -> Dict[str, str]:
    """
    Best-effort projection to {prompt, response} regardless of schema.
    """
    if isinstance(obj, dict):
        # Common patterns
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        # Fallback: look for any text-like fields
        if not prompt or not response:
            textish = [v for k, v in obj.items() if isinstance(v, str) and len(v) > 10]
            if len(textish) >= 2:
                prompt, response = textish[0], textish[1]
            elif len(textish) == 1:
                prompt, response = textish[0], ""
        return {"prompt": str(prompt).strip(), "response": str(response).strip()}

    # Parquet row as tuple-like or scalar
    if isinstance(obj, (list, tuple)) and len(obj) >= 2:
        return {"prompt": str(obj[0]).strip(), "response": str(obj[1]).strip()}
    return {"prompt": str(obj).strip(), "response": ""}

def cdn_download(repo: str, path: str) -> bytes:
    """
    Download via CDN (no auth header) to bypass HF API rate limits.
    """
    url = f"https://huggingface.co/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def load_parquet_via_cdn(repo: str, path: str) -> Iterable[Dict[str, str]]:
    data = cdn_download(repo, path)
    table = pq.read_table(pa.BufferReader(data))
    for batch in table.to_batches(max_chunksize=1000):
        for i in range(batch.num_rows):
            row = {k: batch[k][i].as_py() for k in batch.schema.names}
            yield project_to_pair(row, path)

def load_jsonl_via_cdn(repo: str, path: str) -> Iterable[Dict[str, str]]:
    data = cdn_download(repo, path)
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except
