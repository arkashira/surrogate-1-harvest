# surrogate-1 / discovery

# surrogate-1 — Discovery & Incremental Implementation Plan

## Highest-value incremental improvement (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: one Mac-side API call to `list_repo_tree` → saved `manifest-<date>.json`; workers read manifest and fetch files via **HF CDN URLs** (no Authorization header, bypasses `/api/` rate limits)
- Projects heterogeneous repo files to `{prompt, response}` only at parse time (avoids pyarrow `CastError` from mixed schemas)
- Deduplicates via existing `lib/dedup.py` central md5 store
- Writes normalized JSONL to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
- Keeps idempotent, shard-isolated filenames to avoid commit collisions
- Adds retry/backoff for 429s and respects HF commit cap by using deterministic repo selection if/when spreading across sibling repos

This directly applies lessons:
- HF CDN bypass (THE KEY INSIGHT 2026-04-29)
- Manifest pre-list + embed file list (pre-list once, CDN-only during training)
- Don’t use `load_dataset(streaming=True)` on heterogeneous repos (pyarrow CastError)
- Project to `{prompt, response}` only at parse time (dataset-mirror writes)
- Isolated runners → no OOM (existing runner model preserved)

---

## Concrete implementation plan

1. **Create `bin/dataset-enrich.py`**
   - Shebang `#!/usr/bin/env python3`
   - CLI: `--shard-id`, `--shard-total`, `--date-folder`, `--manifest-path`, `--out-dir`
   - Steps:
     1. Determine `date_folder` (YYYY-MM-DD)
     2. If `--manifest-path` provided, load it; else fetch via HF API `list_repo_tree` for that folder (non-recursive) and save local copy
     3. Filter files by shard: `hash(slug) % SHARD_TOTAL == SHARD_ID`
     4. For each file:
        - Build CDN URL: `f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{date_folder}/{path}"`
        - Download with `requests` (no auth header), timeout/retry
        - Parse based on extension:
          - `.json`/`.jsonl`: try to extract `prompt`/`response` keys; if missing, best-effort field mapping
          - `.parquet`: read with `pyarrow` but project only needed columns; if schema varies, catch `CastError` and fallback to per-file projection
        - Compute md5 of content (or canonical row hash) and call `lib/dedup.py` to check/insert
        - Keep non-duplicate rows, normalize to `{prompt: str, response: str, source_file: str}`
     5. Stream write to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
     6. Upload file to HF dataset repo via `huggingface_hub` (commit with deterministic filename)

2. **Update `lib/dedup.py` (if needed)**
   - Ensure it exposes a simple API: `is_duplicate(md5) -> bool`, `add(md5) -> None`
   - Use SQLite with WAL for concurrent access from multiple runners (each runner isolated, but central store on HF Space is source of truth — workers can still benefit from local cache + eventual central dedup)

3. **Update GitHub Actions matrix (`ingest.yml`)**
   - Keep 16-shard matrix
   - Pass `SHARD_ID`, `SHARD_TOTAL=16`, `DATE_FOLDER` (optional)
   - Ensure `HF_TOKEN` secret available
   - Use `python3 -m pip install -r requirements.txt`
   - Run: `python bin/dataset-enrich.py --shard-id ${{ matrix.shard_id }} --shard-total 16 --date-folder ${{ env.DATE_FOLDER }}`

4. **Add small Mac-side helper script (optional but recommended)**
   - `scripts/build-manifest.py` — run after rate-limit window clears to generate `manifest-YYYY-MM-DD.json` and commit or upload as artifact for workers to consume (reduces API calls further)

5. **Testing & validation**
   - Local dry-run with a small sample folder
   - Verify CDN URLs work without token
   - Confirm dedup integration
   - Run single shard in Actions to validate upload

---

## Code snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage:
  python bin/dataset-enrich.py --shard-id 0 --shard-total 16 --date-folder 2026-05-03
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_upload

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from dedup import DedupStore  # type: ignore

HF_DATASET_REPO = "axentx/surrogate-1-training-pairs"
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
RETRY_BACKOFF = [1, 2, 4, 8, 16]
MAX_RETRIES = len(RETRY_BACKOFF)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CDN-bypass dataset enrich worker")
    parser.add_argument("--shard-id", type=int, required=True, help="Shard index (0..SHARD_TOTAL-1)")
    parser.add_argument("--shard-total", type=int, default=16, help="Total shards")
    parser.add_argument("--date-folder", default=None, help="Date folder YYYY-MM-DD (defaults to today)")
    parser.add_argument("--manifest", default=None, help="Path to manifest JSON (optional)")
    parser.add_argument("--out-dir", default="batches/public-merged", help="Output directory root")
    parser.add_argument("--hf-repo", default=HF_DATASET_REPO, help="HF dataset repo")
    parser.add_argument("--hf-token", default=os.getenv("HF_TOKEN"), help="HF token (env fallback)")
    return parser.parse_args()

def get_date_folder(date_folder: Optional[str]) -> str:
    if date_folder:
        return date_folder
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def build_cdn_url(date_folder: str, rel_path: str) -> str:
    return f"{CDN_BASE}/{date_folder}/{rel_path}"

def load_or_fetch_manifest(
    date_folder: str,
    manifest_path: Optional[str],
    hf_repo: str,
    hf_token: Optional[str],
) -> List[str]:
    """Return list of relative file paths for date_folder."""
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # allow {"files": [...]}
        if isinstance(data, dict) and "files" in data:
            return data["files"]
        raise ValueError("Invalid manifest format")

    # Fetch via HF API (single call)
    api = HfApi(token=hf_token)
    try:
        tree = api.list_repo_tree(repo_id=hf_repo, path=date_folder, recursive=False)
    except Exception as e:
        print(f"Failed to list repo tree: {e}", file=sys.stderr)
        raise

    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    # Save local manifest for reuse/debug
    local_manifest = f"manifest-{date_folder}.json"
    with open(local_manifest, "w") as f:
        json.dump(files, f, indent=2)
    print(f"Saved manifest to {local_manifest}")
    return files

def shard_filter(path: str, shard_id: int, shard_total: int) -> bool:
    """Deterministic shard assignment
