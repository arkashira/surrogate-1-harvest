# surrogate-1 / quality

# surrogate-1 — Quality improvement: manifest-driven, CDN-bypass ingestion worker

## Summary
Replace `bin/dataset-enrich.sh` with a single, robust Python worker (`bin/dataset-enrich.py`) that:
- Uses a **manifest** (`manifests/public-merged/<date>/file-list.json`) generated once per date (Mac orchestrator) to avoid recursive HF API calls and rate limits.
- Downloads dataset files **via HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header** to bypass `/api/` rate limits.
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids pyarrow `CastError`).
- Deduplicates via central md5 store (`lib/dedup.py`) and writes deterministic shard outputs:  
  `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
- Safe for GitHub Actions matrix (16 shards) and local runs; respects Lightning/HF best-practices.

---

## Implementation plan (≤2h)

1. Add `requirements.txt` updates (if missing): `requests tqdm pyarrow`
2. Create `bin/dataset-enrich.py`
   - Accept env: `SHARD_ID` (0..15), `TOTAL_SHARDS` (default 16), `DATE` (YYYY-MM-DD), `DATASET_REPO`, `MANIFEST_PATH`, `HF_TOKEN` (for upload only)
   - Load manifest JSON; deterministic shard assignment by `hash(slug) % TOTAL_SHARDS`
   - Stream each assigned file via CDN URL with `requests` in chunks; parse line-by-line (JSONL) or parquet fallback
   - Project to `{prompt, response}`; normalize keys; skip malformed
   - Dedup via `lib/dedup.py` (md5)
   - Collect rows; write local temp NDJSON; upload to HF dataset repo via `huggingface_hub` (single commit per shard)
   - Retry/backoff on CDN 429/5xx; respect `Retry-After`
3. Replace `bin/dataset-enrich.sh` with a thin wrapper that invokes `python bin/dataset-enrich.py` (preserve existing CI env contract)
4. Update `.github/workflows/ingest.yml`
   - Pass matrix `shard_id` as `SHARD_ID`
   - Pass `DATE` (e.g., today or workflow input)
   - Ensure `MANIFEST_PATH` available (commit manifest in repo or generate on the fly by one job)
5. (Optional) Add small manifest generator script (`bin/gen-manifest.py`) for local/Mac orchestrator use

---

## Code snippets

### bin/dataset-enrich.py
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass dataset enrichment worker.

Usage (env):
  SHARD_ID=0 TOTAL_SHARDS=16 DATE=2026-05-03 \
  DATASET_REPO=axentx/surrogate-1-training-pairs \
  MANIFEST_PATH=manifests/public-merged/2026-05-03/file-list.json \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import time
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from tqdm import tqdm

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import is_duplicate, mark_seen  # type: ignore

# ---------- config ----------
HF_API_BASE = "https://huggingface.co"
CDN_BASE = "https://huggingface.co/datasets"

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
DATE = os.getenv("DATE", "")
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")

if not DATE:
    print("ERROR: DATE (YYYY-MM-DD) is required", file=sys.stderr)
    sys.exit(1)

if not MANIFEST_PATH or not Path(MANIFEST_PATH).exists():
    print(f"ERROR: Manifest not found at {MANIFEST_PATH}", file=sys.stderr)
    sys.exit(1)

# ---------- helpers ----------
def slug_hash_bucket(slug: str, n: int) -> int:
    return int(hashlib.sha256(slug.encode("utf-8")).hexdigest(), 16) % n

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_BASE}/{repo}/resolve/main/{path}"

def backoff_sleep(attempt: int, base: float = 1.0, cap: float = 60.0) -> None:
    t = min(base * (2 ** attempt), cap)
    time.sleep(t)

def stream_cdn_file(url: str, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
    attempt = 0
    while True:
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", "360"))
                    print(f"CDN 429, sleeping {retry_after}s", file=sys.stderr)
                    time.sleep(retry_after)
                    continue
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=chunk_size):
                    yield chunk
                return
        except (requests.RequestException, OSError) as exc:
            attempt += 1
            if attempt > 5:
                raise
            print(f"Download error ({exc}), retry {attempt}/5", file=sys.stderr)
            backoff_sleep(attempt)

def parse_file_to_rows(local_path: Path) -> List[Dict[str, str]]:
    """Parse JSONL or Parquet and project to {prompt, response}."""
    rows: List[Dict[str, str]] = []
    suffix = local_path.suffix.lower()

    try:
        if suffix == ".parquet":
            table = pq.read_table(local_path)
            # Normalize column names
            colmap = {c.lower(): c for c in table.column_names}
            prompt_col = None
            response_col = None
            for pref in ("prompt", "instruction", "question", "input"):
                for c in colmap:
                    if pref in c:
                        prompt_col = colmap[c]
                        break
                if prompt_col:
                    break
            for pref in ("response", "completion", "answer", "output"):
                for c in colmap:
                    if pref in c:
                        response_col = colmap[c]
                        break
                if response_col:
                    break

            if prompt_col is None or response_col is None:
                # fallback: first two string/text cols
                candidates = [c for c in table.column_names if pa.types.is_string(table.schema.field(c).type)]
                if len(candidates) >= 2:
                    prompt_col, response_col = candidates[0], candidates[1]
                else:
                    print(f"WARN: cannot project parquet {local_path}", file=sys.stderr)
                    return rows

            pc = table.column(prompt_col)
            rc = table.column(response_col)
            for i in range(table.num_rows):
                p = pc[i].as_py()
                r = rc[i].as_py()
                if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                    rows.append({"prompt": p.strip(), "response": r.strip()})
            return rows

        # else assume JSONL (or JSON lines in .json)
        with open(local_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Normalize keys
                prompt = None
                response = None
                if isinstance(obj, dict):
                    for k in ("prompt", "instruction", "question", "input"):
                        if k in obj and isinstance(obj[k], str) and obj[k].strip():
                            prompt = obj[k].strip()
                           
