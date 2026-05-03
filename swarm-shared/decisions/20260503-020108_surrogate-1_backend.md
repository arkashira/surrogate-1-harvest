# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from the GitHub Actions matrix.
- Loads a pre-generated `manifest-YYYYMMDD.json` (created by a one-time Mac orchestration script) containing the list of files for the target date folder.
- Uses **CDN-bypass URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) for all downloads — zero HF API calls during ingestion, avoiding 429/128-hr commit limits.
- Projects heterogeneous files to `{prompt, response}` only at parse time (avoids pyarrow CastError from mixed schemas).
- Deduplicates via the existing `lib/dedup.py` central md5 store.
- Writes output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` (parquet later if desired) with no extra metadata columns.
- Reuses the HF token only for the final push (upload) to `axentx/surrogate-1-training-pairs`.
- Adds a small orchestration helper (`bin/gen-manifest.py`) to run on Mac (once per date) to list the folder via HF API and emit the manifest JSON.

### Steps (timed)

1. Inspect current `bin/dataset-enrich.sh` and `lib/dedup.py` (5m).
2. Create `bin/dataset-enrich.py` implementing manifest + CDN bypass + schema projection + dedup + upload (60–75m).
3. Create `bin/gen-manifest.py` for Mac orchestration (10m).
4. Update `.github/workflows/ingest.yml` to use the new python worker and pass matrix vars (10m).
5. Add `requirements.txt` updates if needed (pyarrow, requests, tqdm) (5m).
6. Smoke test locally with a small manifest (15m).

Total: ~2h.

---

## File: bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 20260503 \
    --manifest manifest-20260503.json \
    --out-dir batches/public-merged

Environment:
  HF_TOKEN: HuggingFace write token (for final upload)
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

HF_DATASETS_CDN = "https://huggingface.co/datasets"
RETRY_WAIT = 360  # seconds after 429
DEFAULT_CHUNK_SIZE = 8192

def hf_cdn_url(repo: str, path: str) -> str:
    return f"{HF_DATASETS_CDN}/{repo}/resolve/main/{path}"

def is_retryable(exc: Exception) -> bool:
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))

def download_with_retry(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> bytes:
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
            if resp.status_code == 429:
                wait = RETRY_WAIT
                tqdm.write(f"Rate limited 429 on {url}; waiting {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            out = b""
            for chunk in resp.iter_content(chunk_size=DEFAULT_CHUNK_SIZE):
                out += chunk
            return out
        except Exception as exc:
            if is_retryable(exc) and attempt < 4:
                wait = 2 ** attempt
                tqdm.write(f"Retryable error on {url}: {exc}; wait {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Failed to download after retries: {url}")

def project_to_pair(raw: Dict[str, Any], file_path: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file content to {prompt, response}.
    Supports common patterns seen in surrogate-1 datasets.
    """
    # If already pair-like
    if "prompt" in raw and "response" in raw:
        return {"prompt": str(raw["prompt"]), "response": str(raw["response"])}

    # Common alternate keys
    prompt_keys = {"instruction", "input", "question", "user", "prompt_text"}
    response_keys = {"output", "answer", "assistant", "completion", "response_text"}

    # Try to find one of each
    p = None
    r = None
    for k in raw:
        if p is None and str(k).lower() in prompt_keys:
            p = str(raw[k])
        if r is None and str(k).lower() in response_keys:
            r = str(raw[k])

    if p is not None and r is not None:
        return {"prompt": p, "response": r}

    # Fallback: if exactly two text fields, assign by order
    text_fields = {k: str(v) for k, v in raw.items() if isinstance(v, (str, int, float, bool))}
    if len(text_fields) == 2:
        items = list(text_fields.items())
        return {"prompt": str(items[0][1]), "response": str(items[1][1])}

    # If single text field, treat as prompt with empty response
    if len(text_fields) == 1:
        only = list(text_fields.values())[0]
        return {"prompt": str(only), "response": ""}

    tqdm.write(f"Could not project pair from {file_path}: keys={list(raw.keys())}")
    return None

def parse_file_content(content: bytes, file_path: str) -> Iterable[Dict[str, str]]:
    """
    Lightweight parser that attempts JSONL, JSON, and parquet.
    Projects each record to {prompt, response}.
    """
    suffix = Path(file_path).suffix.lower()

    # Parquet
    if suffix == ".parquet":
        try:
            table = pq.read_table(pa.BufferReader(content))
            df = table.to_pylist()
            for row in df:
                pair = project_to_pair(row, file_path)
                if pair:
                    yield pair
            return
        except Exception as exc:
            tqdm.write(f"Parquet parse failed for {file_path}: {exc}")
            return

    # Try JSONL first (most common)
    text = content.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # If single-line JSON array, treat as JSON
    if len(lines) == 1 and lines[0].startswith("["):
        try:
            data = json.loads(lines[0])
            if isinstance(data, list):
                for item in data:
                    pair = project_to_pair(item, file_path)
                    if pair:
                        yield pair
                return
        except Exception:
            pass

    # JSONL
    for i, line in enumerate(lines):
        try:
            item = json.loads(line)
            pair = project_to_pair(item, file_path)
            if pair:
                yield pair
        except Exception as exc:
            tqdm.write(f"JSONL parse failed line {i} in {file_path}: {exc}")
            continue

def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def worker(
    repo: str,
    manifest_path: Path,
    shard_id: int,
    shard_total: int,
    date_str: str,
    out_dir: Path,
    hf_token: str,
) -> None:
    with manifest_path.open() as f:
        manifest = json.load(f)

    # manifest format: {"date": "2026050
