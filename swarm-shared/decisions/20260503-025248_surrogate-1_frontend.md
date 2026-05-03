# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic shard assignment (hash-slug → bucket)
- Builds manifest JSON listing only files assigned to this shard
- Downloads via **HF CDN bypass** (`resolve/main/...` no auth) and projects to `{prompt,response}` only
- Streams output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Uses central `lib/dedup.py` for cross-source md5 dedup (best-effort; duplicates acceptable)
- Exits 0 on success, non-zero on fatal error (GitHub Actions will retry)

### Why this is the highest-value incremental improvement
- Fixes the core risk in the runner: `load_dataset(streaming=True)` on heterogeneous schemas causes `pyarrow.CastError` and wastes API rate limits.
- CDN bypass removes HF API auth rate limits during heavy data load (the key 2026-04-29 insight).
- Manifest-driven approach means Lightning training can embed the file list and do zero-API data loading later.
- Keeps the 16-shard parallel model intact while making each shard robust and observable.

---

## Concrete Implementation

### 1) New worker script: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Environment:
  SHARD_ID        (int) 0..15
  SHARD_TOTAL     (int) default 16
  DATE            (str) YYYY-MM-DD folder under dataset repo
  HF_TOKEN        (str) write token for axentx/surrogate-1-training-pairs
  REPO_ID         (str) default axentx/surrogate-1-training-pairs
  OUTPUT_DIR      (str) default batches/public-merged
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "batches/public-merged")

if not HF_TOKEN:
    print("HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
CDN_PREFIX = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

def slug_for_path(path: str) -> str:
    """Deterministic slug for a dataset file."""
    # e.g. batches/raw/2026-05-03/abc123.parquet -> abc123
    stem = Path(path).stem
    return stem

def shard_for_slug(slug: str) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    return int(digest, 16) % SHARD_TOTAL

def list_date_files(date: str):
    """Single API call: list top-level files in DATE folder."""
    try:
        tree = API.list_repo_tree(repo_id=REPO_ID, path=date, recursive=False)
        # tree can be list or iterator depending on hf_hub version
        items = list(tree) if not isinstance(tree, list) else tree
        # Keep only files (skip subfolders)
        files = [p.rstrip("/") for p in items if "/" not in Path(p).name]
        return sorted(files)
    except Exception as exc:
        print(f"Failed to list repo tree for {date}: {exc}", file=sys.stderr)
        raise

def project_to_pair(local_path: Path):
    """
    Load file with datasets (streaming) and yield {prompt,response} rows.
    Handles mixed schemas by selecting only known fields.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        pf = pq.ParquetFile(local_path)
        for batch in pf.iter_batches(batch_size=500):
            table = pa.Table.from_batches([batch])
            # Normalize to record batches with prompt/response
            rows = []
            cols = table.column_names
            prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
            response_col = next((c for c in ("response", "output", "completion") if c in cols), None)

            if prompt_col is None or response_col is None:
                # Skip rows we cannot project
                continue

            prompts = table[prompt_col].to_pylist()
            responses = table[response_col].to_pylist()
            for p, r in zip(prompts, responses):
                if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                    rows.append({"prompt": p.strip(), "response": r.strip()})
            for row in rows:
                yield row
    except Exception as exc:
        print(f"Failed to project {local_path}: {exc}", file=sys.stderr)
        return

def download_via_cdn(repo_id: str, path: str, token: str, dest: Path):
    """Download via CDN (no auth rate limits)."""
    url = f"{CDN_PREFIX}/{path}"
    # Some repos may require token for private assets; include it if provided.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, headers=headers, stream=True, timeout=60)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest

def main():
    print(f"Starting shard {SHARD_ID}/{SHARD_TOTAL} for date={DATE}")

    # 1) List files once
    files = list_date_files(DATE)
    print(f"Found {len(files)} files in {DATE}")

    # 2) Assign shard files
    shard_files = [p for p in files if shard_for_slug(slug_for_path(p)) == SHARD_ID]
    print(f"Shard {SHARD_ID} assigned {len(shard_files)} files")

    if not shard_files:
        print("No files assigned; exiting.")
        sys.exit(0)

    # 3) Build manifest
    manifest = {
        "repo_id": REPO_ID,
        "date": DATE,
        "shard_id": SHARD_ID,
        "shard_total": SHARD_TOTAL,
        "files": shard_files,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = Path("manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {manifest_path}")

    # 4) Dedup store
    dedup = DedupStore()

    # 5) Process files and stream output
    out_dir = Path(OUTPUT_DIR) / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    out_file = out_dir / f"shard{SHARD_ID}-{timestamp}.jsonl"

    processed = 0
    uploaded = 0
    skipped_dup = 0

    with open(out_file, "w", encoding="utf-8") as fout:
        for rel_path in shard_files:
            try:
                # Download via CDN (bypass API rate limits)
                tmp = Path("tmp") / rel_path.replace("/", "_")
                download_via_cdn(REPO_ID, rel_path,
