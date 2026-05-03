# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side, one `list_repo_tree` call per date folder) to deterministically shard files without recursive API pagination.
- Downloads only assigned files via HF CDN (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API calls during training/ingest, bypassing 429 limits.
- Projects each file to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` on mixed-schema repos) and normalizes to surrogate-1 schema.
- Deduplicates via central `lib/dedup.py` md5 store and writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Adds lightweight retry/backoff for CDN 429/5xx and respects HF commit cap by using deterministic filenames (no collisions across shards/iterations).

### Steps (1h45m total)

1. Create `bin/dataset-enrich.py` (60m) — manifest loading, CDN fetch, schema projection, shard assignment, dedup, write.
2. Update GitHub Actions matrix to pass `SHARD_ID`/`SHARD_TOTAL` and optional `FILE_MANIFEST` path (15m).
3. Add small util to generate `file-list.json` on Mac (`scripts/gen-file-list.py`) (20m) — single `list_repo_tree` per date folder, saved for reuse.
4. Smoke-test locally with a tiny sample (10m).

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass, manifest-driven ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 \
  FILE_MANIFEST=file-list.json \
  HF_DATASET="axentx/surrogate-1-training-pairs" \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrogate-1.py
"""

import os
import sys
import json
import hashlib
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

import requests
import pyarrow.parquet as pq
import pyarrow as pa
import numpy as np

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-enrich")

# Constants
CDN_BASE = "https://huggingface.co/datasets"
RETRY_BACKOFF = [1, 2, 4, 8, 16]
MAX_RETRIES = len(RETRY_BACKOFF)
BATCH_SIZE = 500  # rows per write chunk

def deterministic_shard(key: str, total: int) -> int:
    """Map key to shard by md5 hash."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest, 16) % total

def load_manifest(path: Path) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Expected: list of relative file paths under dataset repo
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    if isinstance(data, list):
        return data
    raise ValueError(f"Unexpected manifest format: {path}")

def cdn_url(repo: str, filepath: str) -> str:
    return f"{CDN_BASE}/{repo}/resolve/main/{filepath}"

def download_file(url: str, timeout: int = 30) -> bytes:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=timeout, stream=False)
            if resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", RETRY_BACKOFF[attempt]))
                log.warning("CDN 429, waiting %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = RETRY_BACKOFF[attempt]
            log.warning("Download failed (%s), retry in %ss: %s", exc, wait, url)
            time.sleep(wait)
    raise RuntimeError(f"Failed to download after retries: {url}")

def project_to_pair(obj: Dict[str, Any], source_file: str) -> Optional[Dict[str, Any]]:
    """
    Project heterogeneous file content to surrogate-1 {prompt,response}.
    Heuristic: look for common field names; fallback to raw text fields.
    """
    # Normalize keys to lower for matching
    low = {k.lower(): v for k, v in obj.items() if isinstance(k, str)}

    prompt = None
    response = None

    # Common patterns
    for pkey in ("prompt", "instruction", "question", "input", "user", "query"):
        if pkey in low and isinstance(low[pkey], str) and low[pkey].strip():
            prompt = low[pkey].strip()
            break
    for rkey in ("response", "completion", "answer", "output", "assistant", "text"):
        if rkey in low and isinstance(low[rkey], str) and low[rkey].strip():
            response = low[rkey].strip()
            break

    # Fallback: if object has exactly two string fields, assign shorter as prompt
    if prompt is None or response is None:
        str_fields = [v for v in low.values() if isinstance(v, str) and v.strip()]
        if len(str_fields) == 2:
            a, b = str_fields[0].strip(), str_fields[1].strip()
            prompt, response = (a, b) if len(a) <= len(b) else (b, a)

    if not prompt or not response:
        log.debug("Skipping row from %s: could not project to prompt/response", source_file)
        return None

    # Build surrogate-1 schema (no source/ts columns; attribution via filename pattern)
    return {
        "prompt": prompt,
        "response": response,
    }

def parse_parquet(content: bytes, source_file: str) -> List[Dict[str, Any]]:
    table = pq.read_table(pa.py_buffer(content))
    df = table.to_pandas()
    pairs = []
    for _, row in df.iterrows():
        obj = row.to_dict()
        pair = project_to_pair(obj, source_file)
        if pair:
            pairs.append(pair)
    return pairs

def parse_jsonl(content: bytes, source_file: str) -> List[Dict[str, Any]]:
    pairs = []
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        pair = project_to_pair(obj, source_file)
        if pair:
            pairs.append(pair)
    return pairs

def parse_file(content: bytes, filepath: str, source_file: str) -> List[Dict[str, Any]]:
    if filepath.endswith(".parquet"):
        return parse_parquet(content, source_file)
    if filepath.endswith(".jsonl"):
        return parse_jsonl(content, source_file)
    log.warning("Unsupported file type: %s", filepath)
    return []

def write_shard(output_dir: Path, shard_id: int, rows: List[Dict[str, Any]], date_str: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = output_dir / f"shard{shard_id}-{ts}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info("Wrote %d rows to %s", len(rows), out_path)

def
