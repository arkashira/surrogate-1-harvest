# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.sh`** → **`bin/dataset-enrich.py`**
   - Single `list_repo_tree(path, recursive=False)` call from Mac (or runner) for one date folder; emit `manifest.json` with CDN URLs.
   - Worker downloads via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no Authorization header → bypasses `/api/` rate limit).
   - Per-record schema projection to `{prompt, response}` only at parse time; drop all other fields to avoid `pyarrow.CastError` on heterogeneous files.
   - Deterministic `slug-hash → shard_id` routing (same as current `SHARD_ID` logic) so 16 runners stay non-overlapping.
   - Central md5 dedup via existing `lib/dedup.py` (SQLite) to preserve cross-run de-duplication.
   - Output `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` (unchanged format) to keep downstream training pipelines stable.

2. **`lib/dedup.py`** (no change) — keep as source-of-truth SQLite store; workers will connect to it if available, otherwise run local dedup and rely on filename-based de-dup at merge time (acceptable trade-off per README).

3. **`.github/workflows/ingest.yml`**
   - Update matrix runner to use `python3 -m bin.dataset_enrich --shard ${{ matrix.shard }} --date ${{ inputs.date || github.run_id }}`.
   - Keep 16-shard parallelism; each job gets isolated 7 GB runner.
   - Add retry/backoff for 429 on the initial `list_repo_tree` call (wait 360s) — CDN downloads themselves won’t hit the API limit.

4. **`requirements.txt`**
   - Add `requests`, keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`.

5. **`train.py`** (optional, if present) — embed the generated `manifest.json` so Lightning Studio does **CDN-only** fetches with zero API calls during data load (per pattern).

### Code snippets

#### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-first, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  python -m bin.dataset_enrich --shard 0 --date 2026-05-03
"""

import argparse
import hashlib
import json
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
from huggingface_hub import list_repo_tree, hf_hub_download

REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"
HF_TOKEN = os.getenv("HF_TOKEN")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

# Local imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa: E402

DATE_FORMAT = "%Y-%m-%d"
OUTPUT_DIR = Path("batches/public-merged")

# Deterministic shard assignment: 16 shards
N_SHARDS = 16

def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def assign_shard(slug: str) -> int:
    return slug_hash(slug) % N_SHARDS

def list_date_folder(date_str: str) -> List[str]:
    """Single API call to list one date folder; retry on 429."""
    path = f"batches/mirror-merged/{date_str}"
    for attempt in range(5):
        try:
            tree = list_repo_tree(repo_id=REPO, path=path, recursive=False, token=HF_TOKEN)
            files = [item.rfilename for item in tree if item.rfilename.endswith(".parquet")]
            return files
        except Exception as exc:
            if hasattr(exc, "response") and getattr(exc.response, "status_code", None) == 429:
                wait = 360
                print(f"HF API 429, waiting {wait}s (attempt {attempt+1})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Exhausted retries on HF API 429")

def download_cdn(url: str, dest: Path) -> None:
    """CDN download (no Authorization header) to bypass API rate limits."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)

def project_to_pair(batch: Dict[str, Any]) -> Dict[str, str]:
    """
    Schema projection: keep only prompt/response fields.
    Tolerates heterogeneity by dropping unknown columns.
    """
    prompt = batch.get("prompt") or batch.get("input") or batch.get("question") or ""
    response = batch.get("response") or batch.get("output") or batch.get("answer") or ""
    # Normalize to string
    prompt = "" if prompt is None else str(prompt).strip()
    response = "" if response is None else str(response).strip()
    return {"prompt": prompt, "response": response}

def extract_pairs_from_parquet(path: Path) -> List[Dict[str, str]]:
    """Read parquet and project each row to {prompt, response}."""
    try:
        table = pq.read_table(path, columns=["prompt", "response"])
    except (pa.ArrowInvalid, KeyError, OSError):
        # Fallback: read all and project
        table = pq.read_table(path)
    # Convert to list of dicts safely
    cols = table.column_names
    pairs = []
    for i in range(table.num_rows):
        row = {c: table[c][i].as_py() for c in cols}
        pairs.append(project_to_pair(row))
    return pairs

def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-bypass ingestion worker")
    parser.add_argument("--shard", type=int, required=True, help="Shard ID (0..15)")
    parser.add_argument("--date", type=str, required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR, help="Output root")
    args = parser.parse_args()

    if not (0 <= args.shard < N_SHARDS):
        print(f"Invalid shard {args.shard}; must be 0..{N_SHARDS-1}", file=sys.stderr)
        sys.exit(1)

    try:
        datetime.strptime(args.date, DATE_FORMAT)
    except ValueError:
        print(f"Invalid date {args.date}; expected YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    dedup = DedupStore()
    files = list_date_folder(args.date)
    print(f"Found {len(files)} parquet files for {args.date}", file=sys.stderr)

    # Build manifest (for reproducibility / Lightning training)
    manifest = {
        "date": args.date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shard": args.shard,
        "files": [],
    }

    out_dir = args.out_dir / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%H%M%S")
    outfile = out_dir / f"shard{args.shard}-{timestamp}.jsonl"

    accepted = 0
    skipped_dup = 0
    skipped_shard = 0

    with outfile.open("w", encoding="utf-8") as fout:
        for rel in files:
            slug = Path(rel).stem
            if assign_shard(slug) != args.shard:
                skipped_shard += 1
                continue

            local_path = Path("tmp") / Path(rel).name
            local_path.parent.mkdir(parents=True, exist_ok=True)

            cdn_url = f"{BASE_CDN}/{
