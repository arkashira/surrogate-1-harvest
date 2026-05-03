# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — manifest-driven, CDN-only fetcher with schema projection and deterministic shard routing.
2. **Update `bin/dataset-enrich.sh`** — thin wrapper that invokes `python bin/worker.py` with proper env and error handling.
3. **Update `.github/workflows/ingest.yml`** — ensure `SHELL=/bin/bash` and invoke via `bash bin/dataset-enrich.sh`.
4. **Add `requirements.txt` update** — include `requests` if not present.

---

### 1) `bin/worker.py`

```python
#!/usr/bin/env python3
"""
Surrogate-1 shard worker (CDN-bypass).

- Reads a pre-computed file manifest (JSON) listing parquet files for one date folder.
- Each worker processes only files assigned to its SHARD_ID (0-15) by slug-hash.
- Downloads via HF CDN (no Authorization header) to bypass API rate limits.
- Projects to {prompt, response} only; writes to batches/public-merged/<date>/shard<N>-<TS>.jsonl
- Emits structured logs and non-zero exit on fatal errors.
"""
import json
import os
import sys
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Dict, Any

import pyarrow.parquet as pq
import pyarrow as pa
import requests

# ---- config ----
HF_DATASET = os.getenv("HF_DATASET", "axentx/surrogate-1-training-pairs")
HF_REPO_TYPE = os.getenv("HF_REPO_TYPE", "dataset")
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
NUM_SHARDS = int(os.getenv("NUM_SHARDS", "16"))
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "")  # e.g. manifest-2026-05-03.json
DATE_FOLDER = os.getenv("DATE_FOLDER", "")     # e.g. 2026-05-03
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "batches/public-merged")

# ---- logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("worker")

# ---- utils ----
def slug_hash(slug: str) -> int:
    """Deterministic 0..65535 hash for sharding."""
    return int(hashlib.sha256(slug.encode("utf-8")).hexdigest(), 16) % 65536

def assign_shard(slug: str, num_shards: int = NUM_SHARDS) -> int:
    return slug_hash(slug) % num_shards

def cdn_url(path: str) -> str:
    return CDN_TEMPLATE.format(repo=HF_DATASET, path=path)

def safe_request(url: str, timeout: int = 30) -> requests.Response:
    resp = requests.get(url, timeout=timeout, headers={})
    resp.raise_for_status()
    return resp

# ---- schema projection ----
def project_record(batch: pa.RecordBatch) -> Iterator[Dict[str, Any]]:
    """
    Project heterogeneous parquet to {prompt, response}.
    Accepts common field names (case-insensitive).
    """
    cols = {c.lower(): c for c in batch.schema.names}
    prompt_col = None
    response_col = None

    for key, norm in [("prompt", "prompt"), ("question", "prompt"), ("input", "prompt"),
                      ("response", "response"), ("answer", "response"), ("output", "response")]:
        if key in cols:
            if norm == "prompt" and prompt_col is None:
                prompt_col = cols[key]
            elif norm == "response" and response_col is None:
                response_col = cols[key]

    # fallback positional: first text col as prompt, second as response
    if prompt_col is None or response_col is None:
        text_cols = [c for c in batch.schema.names if pa.types.is_string(batch.schema.field(c).type)]
        if prompt_col is None and len(text_cols) > 0:
            prompt_col = text_cols[0]
        if response_col is None and len(text_cols) > 1:
            response_col = text_cols[1]

    if prompt_col is None or response_col is None:
        log.warning("Could not resolve prompt/response columns in %s", batch.schema)
        return

    n = batch.num_rows
    prompts = batch.column(prompt_col).to_pylist()
    responses = batch.column(response_col).to_pylist()
    for i in range(n):
        p = prompts[i]
        r = responses[i]
        if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
            yield {"prompt": p.strip(), "response": r.strip()}

# ---- worker ----
def process_file(remote_path: str, out_f) -> int:
    url = cdn_url(remote_path)
    log.info("Fetching %s", remote_path)
    try:
        resp = safe_request(url, timeout=60)
    except Exception as exc:
        log.error("Failed to fetch %s: %s", remote_path, exc)
        return 0

    written = 0
    try:
        with pa.BufferReader(resp.content) as buf:
            pf = pq.ParquetFile(buf)
            for batch in pf.iter_batches(batch_size=1024):
                for rec in project_record(batch):
                    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
    except Exception as exc:
        log.error("Failed to decode %s: %s", remote_path, exc)
        return written

    log.info("Wrote %d records from %s", written, remote_path)
    return written

def run() -> int:
    if not MANIFEST_PATH or not Path(MANIFEST_PATH).exists():
        log.error("MANIFEST_PATH missing or not found: %s", MANIFEST_PATH)
        return 1
    if not DATE_FOLDER:
        log.error("DATE_FOLDER required")
        return 1

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    files = manifest.get("files", [])
    if not files:
        log.warning("No files in manifest")
        return 0

    my_files = [f for f in files if assign_shard(f) == SHARD_ID]
    log.info("Shard %d/%d assigned %d/%d files", SHARD_ID, NUM_SHARDS, len(my_files), len(files))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = Path(OUTPUT_DIR) / DATE_FOLDER / f"shard{SHARD_ID}-{ts}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with out_path.open("w", encoding="utf-8") as out_f:
        for remote_path in my_files:
            total += process_file(remote_path, out_f)

    log.info("Shard %d complete. Total records: %d -> %s", SHARD_ID, total, out_path)
    return 0

if __name__ == "__main__":
    sys.exit(run())
```

---

### 2) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# Surrogate-1 shard worker wrapper (CDN-bypass).
# Invoked by GitHub Actions matrix job.
#
# Required env:
#   SHARD_ID          (0-15)
#   NUM_SHARDS        (default 16)
#   DATE_FOLDER       (e.g. 2026-05-03)
#   MANIFEST_PATH     (path to JSON file listing parquet files)
#   HF_DATASET        (default axentx/surrogate-1-training-pairs)
#   OUTPUT_DIR        (default batches/public-merged)
#
# Behavior:
#   Runs bin/worker.py and exits with the same
