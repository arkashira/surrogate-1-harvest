# surrogate-1 / frontend

## Final Implementation Plan (≤2 h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that is safe for GitHub Actions matrix (16 shards), deterministic, and zero-training-API-pressure.

### Core behavior (merged + resolved)
- **Inputs**: `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (optional for write), `HF_REPO` (default `datasets/axentx/surrogate-1-training-pairs`), optional `--manifest-file`.
- **Listing**: one `list_repo_tree(path=DATE, recursive=False)` (or cached manifest) to enumerate files in `DATE/`. Deterministic shard assignment via `hash(slug) % SHARD_TOTAL`.
- **Download**: CDN-only (`https://huggingface.co/datasets/.../resolve/main/...`). No Authorization header for downloads (zero API rate-limit pressure). If repo is private, use token only for listing/upload; downloads still via CDN with token-in-URL only when strictly required (kept explicit).
- **Parsing**: stream each file and project to `{prompt, response}` at parse time. Do **not** rely on `load_dataset(streaming=True)` on heterogeneous schemas (avoids mixed-schema failures). Implement per-format lightweight streaming readers (JSONL, JSON, Parquet via `pyarrow`/`pandas` chunks) with a small, explicit projection layer.
- **Deduplication**: central md5 store (`lib/dedup.py`) before emitting.
- **Output**: newline-delimited JSON to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`. Optional upload: one commit per shard per run via HF API.
- **Exit**: clear status codes and structured logs; safe for CI matrix.

Time budget: ~90 minutes (60m implementation + 30m smoke test).

---

## Changes

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (local):
  DATE=2026-05-03 SHARD_ID=0 SHARD_TOTAL=16 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Usage (CI):
  python bin/dataset-enrich.py \
    --shard-id "$SHARD_ID" \
    --shard-total "$SHARD_TOTAL" \
    --date "$DATE" \
    --hf-token "$HF_TOKEN" \
    --hf-repo "datasets/axentx/surrogate-1-training-pairs" \
    --manifest-file manifest.json

If --manifest-file is provided, skip list_repo_tree and use that list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import requests
from huggingface_hub import HfApi, list_repo_tree

# Project-local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

HF_REPO_DEFAULT = "datasets/axentx/surrogate-1-training-pairs"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
BATCH_DIR_TEMPLATE = "batches/public-merged/{date}"

# Deterministic shard assignment
def shard_for_slug(slug: str, shard_total: int) -> int:
    digest = hashlib.md5(slug.encode("utf-8")).hexdigest()
    return int(digest, 16) % shard_total

def build_file_list_api(date: str, repo: str, hf_token: Optional[str]) -> List[str]:
    """
    Single API call: list files in DATE folder (non-recursive).
    Returns list of repo-relative paths.
    """
    try:
        items = list_repo_tree(
            repo_id=repo,
            path=date,
            repo_type="dataset",
            token=hf_token,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to list repo tree for {repo}/{date}: {exc}") from exc

    paths = [it["path"] for it in items if it.get("type") == "file"]
    if not paths:
        print(f"[WARN] No files found in {repo}/{date}", file=sys.stderr)
    return paths

def build_file_list_manifest(manifest_path: Path) -> List[str]:
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    if isinstance(data, list):
        return data
    raise ValueError(f"Invalid manifest format: {manifest_path}")

def cdn_download_urls(paths: List[str], repo: str) -> Generator[Dict[str, str], None, None]:
    for p in paths:
        slug = Path(p).stem
        url = CDN_TEMPLATE.format(repo=repo, path=p)
        yield {"path": p, "slug": slug, "url": url}

# Lightweight streaming parsers (avoid load_dataset on mixed schemas)
def stream_jsonl_lines(url: str) -> Generator[Dict[str, Any], None, None]:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj
            except Exception:
                continue

def stream_json_lines(url: str) -> Generator[Dict[str, Any], None, None]:
    # Some files may be JSON arrays; stream by parsing whole doc only if small.
    # For safety, prefer JSONL; fallback to requests + ijson if needed.
    # Keep simple: if JSONL fails, skip.
    try:
        yield from stream_jsonl_lines(url)
    except Exception:
        pass

def stream_parquet_rows(url: str, chunk_size: int = 50_000) -> Generator[Dict[str, Any], None, None]:
    # Use pandas/pyarrow streaming via chunks; avoid loading full file.
    import pandas as pd
    # Download to temp file for pyarrow compatibility
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        for batch in pd.read_parquet(tmp_path, chunksize=chunk_size):
            for _, row in batch.iterrows():
                yield row.to_dict()
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

def parse_file_to_pairs(file_path: str, url: str) -> Generator[Dict[str, str], None, None]:
    """
    Stream a single file and project to {prompt, response}.
    Handles mixed schemas by selecting known fields.
    """
    lower = file_path.lower()
    stream: Optional[Generator[Dict[str, Any], None, None]] = None

    if lower.endswith(".jsonl"):
        stream = stream_jsonl_lines(url)
    elif lower.endswith(".json"):
        stream = stream_json_lines(url)
    elif lower.endswith(".parquet"):
        stream = stream_parquet_rows(url)
    else:
        # Unknown; attempt JSONL first
        stream = stream_jsonl_lines(url)

    if stream is None:
        return

    for row in stream:
        if not isinstance(row, dict):
            continue

        # Surrogate-1 projection: prompt/response preferred keys
        prompt = row.get("prompt") or row.get("input") or row.get("question") or row.get("text")
        response = row.get("response") or row.get("output") or row.get("answer") or row.get("completion")

        if prompt is None or response is None:
            continue

        yield {"prompt": str(prompt), "response": str(response)}

def upload_batch(
    hf_api: HfApi,
    repo: str,
    date: str,

