# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date on the Mac orchestrator and committed to the repo (or passed via `file_list.json`). Workers skip recursive `list_repo_files` API calls entirely and use CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for zero-auth, high-rate downloads.
- Projects heterogeneous source files to `{prompt, response}` only at parse time, writes normalized JSONL to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Keeps the existing central `lib/dedup.py` md5 store contract (SQLite) for cross-run dedup.
- Adds retry/backoff for CDN 429s and respects HF commit-cap strategy (unique filenames per shard+timestamp).
- Uploads JSONL to HF dataset repo using `huggingface_hub` (atomic, retries, optional PR).

### Steps (timed)

1. **Create `bin/dataset-enrich.py`** (60 min) — manifest loader, CDN downloader, schema projector, shard routing, JSONL writer, HF uploader.
2. **Update `.github/workflows/ingest.yml`** (15 min) — pass `MANIFEST_PATH` (or embed date), set `HF_TOKEN`, matrix `shard_id: [0..15]`.
3. **Add small util `bin/gen-manifest.py`** (15 min) — one-off Mac script to run `list_repo_tree` for a date folder and save `manifests/YYYY-MM-DD.json`.
4. **Remove/disable old `bin/dataset-enrich.sh`** (10 min) — keep as backup or delete; update README if needed.

Total: ~100 min (safe within 2h).

---

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (GitHub Actions matrix):
  python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --shard-id ${{ matrix.shard_id }} \
    --shard-total 16 \
    --manifest manifests/2026-05-03.json \
    --out-dir batches/public-merged

Environment:
  HF_TOKEN: write token for pushing JSONL outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm
from huggingface_hub import HfApi, Repository, hf_hub_download

# Local dedup contract
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
RETRY_BACKOFF = [1, 2, 4, 8, 16]
MAX_RETRIES = len(RETRY_BACKOFF)


def deterministic_shard(path: str, shard_total: int) -> int:
    """Map path to shard by md5 hash."""
    digest = hashlib.md5(path.encode("utf-8")).hexdigest()
    return int(digest, 16) % shard_total


def load_manifest(manifest_path: Path) -> List[str]:
    """Load list of dataset file paths from JSON manifest."""
    with manifest_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    # Manifest format: list of relative paths OR {"paths": [...]}
    if isinstance(data, dict) and "paths" in data:
        return data["paths"]
    if isinstance(data, list):
        return data
    raise ValueError(f"Unexpected manifest format in {manifest_path}")


def download_via_cdn(
    repo: str,
    path: str,
    client: httpx.Client,
    timeout: float = 30.0,
) -> Optional[bytes]:
    """Download a single file via HF CDN (no Authorization header)."""
    url = HF_CDN_TEMPLATE.format(repo=repo, path=path)
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url, timeout=timeout, follow_redirects=True)
            if resp.status_code == 200:
                return resp.content
            # CDN 429 or 5xx -> backoff
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = RETRY_BACKOFF[attempt]
                time.sleep(wait)
                continue
            # 404/403 etc -> skip
            print(f"Skipping {path}: HTTP {resp.status_code}", file=sys.stderr)
            return None
        except httpx.RequestError as exc:
            wait = RETRY_BACKOFF[attempt]
            print(f"Network error for {path}: {exc}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    print(f"Failed to download {path} after {MAX_RETRIES} retries", file=sys.stderr)
    return None


def project_to_pair(raw_bytes: bytes, path: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response}.
    Supports: .jsonl, .parquet, .json (basic).
    Returns None if unparseable.
    """
    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".parquet":
            table = pq.read_table(pa.BufferReader(raw_bytes))
            cols = table.column_names
            prompt_col = next((c for c in ("prompt", "input", "question") if c in cols), None)
            response_col = next((c for c in ("response", "output", "answer") if c in cols), None)
            if prompt_col and response_col:
                prompts = table.column(prompt_col).to_pylist()
                responses = table.column(response_col).to_pylist()
            elif len(cols) >= 2:
                prompts = table.column(cols[0]).to_pylist()
                responses = table.column(cols[1]).to_pylist()
            else:
                return None
            for p, r in zip(prompts, responses):
                if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                    return {"prompt": p.strip(), "response": r.strip()}
            return None

        if suffix == ".jsonl":
            for line in raw_bytes.decode("utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                response = obj.get("response") or obj.get("output") or obj.get("answer")
                if isinstance(prompt, str) and isinstance(response, str) and prompt.strip() and response.strip():
                    return {"prompt": prompt.strip(), "response": response.strip()}
            return None

        if suffix == ".json":
            obj = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
            if isinstance(obj, list) and len(obj) > 0:
                obj = obj[0]
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if isinstance(prompt, str) and isinstance(response, str) and prompt.strip() and response.strip():
                return {"prompt": prompt.strip(), "response": response.strip()}
            return None

    except Exception as exc:
        print(f"Projection failed for {path}: {exc}", file=sys.stderr)
    return None


def upload_jsonl_via_hf_api(
    repo: str,
    local_path: Path,
    hf_token: str,
    commit_message: str,
    create_pr: bool = False,
) -> None:
    """
    Upload JSONL to HF dataset repo using huggingface_hub.
    """
    api =
