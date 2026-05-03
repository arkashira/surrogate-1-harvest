# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side `list_repo_tree` snapshot) to deterministically assign 1/16 of files per shard — no recursive API calls during ingestion.
- Downloads assigned files via HF CDN (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header** → bypasses 429 rate limits.
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas).
- Deduplicates via central `lib/dedup.py` md5 store.
- Writes output as `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` and streams upload via HF Hub (one commit per shard).
- Adds retry/backoff for CDN 429/5xx and respects HF commit cap by using deterministic shard → repo mapping if scaled to siblings later.

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage (local/test):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --file-list file-list.json \
    --out-dir batches/public-merged

GitHub Actions sets SHARD_ID/SHARD_TOTAL automatically.
"""
import argparse
import base64
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

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa: E402

HF_DATASETS_CDN = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
RETRY_WAIT = 5
MAX_RETRIES = 5
BACKOFF_FACTOR = 2

# Deterministic shard assignment
def assign_shard(key: str, shard_total: int) -> int:
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest, "little") % shard_total

def load_file_list(path: str) -> List[str]:
    with open(path) as f:
        data = json.load(f)
    # Accept either list of paths or object with "files" key
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    return data

def cdn_download(client: httpx.Client, repo: str, file_path: str, retries: int = MAX_RETRIES) -> Optional[bytes]:
    url = HF_DATASETS_CDN.format(repo=repo, path=file_path)
    for attempt in range(1, retries + 1):
        try:
            resp = client.get(url, timeout=30.0)
            if resp.status_code == 200:
                return resp.content
            # CDN 429/5xx -> backoff
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = RETRY_WAIT * (BACKOFF_FACTOR ** (attempt - 1))
                tqdm.write(f"CDN {resp.status_code} for {file_path}, retry {attempt}/{retries} in {wait}s")
                time.sleep(wait)
                continue
            # 404/403 are unlikely for public files; skip
            tqdm.write(f"Unexpected {resp.status_code} for {file_path}: {resp.text[:200]}")
            return None
        except httpx.RequestError as exc:
            wait = RETRY_WAIT * (BACKOFF_FACTOR ** (attempt - 1))
            tqdm.write(f"Request error for {file_path}: {exc}, retry {attempt}/{retries} in {wait}s")
            time.sleep(wait)
    return None

def project_to_pair(content: bytes, file_path: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response}.
    Supports:
      - Parquet (HF datasets)
      - JSON/JSONL
    Returns None if projection fails.
    """
    name = file_path.lower()
    try:
        if name.endswith(".parquet"):
            tbl = pq.read_table(pa.BufferReader(content))
            # Keep only prompt/response columns if present; else first two string cols
            cols = tbl.column_names
            prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
            response_col = next((c for c in ("response", "output", "completion") if c in cols), None)
            if prompt_col and response_col:
                prompts = tbl.column(prompt_col).to_pylist()
                responses = tbl.column(response_col).to_pylist()
            elif len(cols) >= 2:
                # fallback: first two cols
                prompts = tbl.column(cols[0]).to_pylist()
                responses = tbl.column(cols[1]).to_pylist()
            else:
                return None
            # Return first valid pair
            for p, r in zip(prompts, responses):
                if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                    return {"prompt": p.strip(), "response": r.strip()}
            return None

        # JSON/JSONL
        text = content.decode("utf-8", errors="replace")
        if name.endswith(".jsonl"):
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            objs = [json.loads(ln) for ln in lines]
        else:
            objs = json.loads(text)
            if not isinstance(objs, list):
                # Allow single object wrapped in list
                objs = [objs]

        for obj in objs:
            if not isinstance(obj, dict):
                continue
            prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input")
            response = obj.get("response") or obj.get("output") or obj.get("completion")
            if isinstance(prompt, str) and isinstance(response, str) and prompt.strip() and response.strip():
                return {"prompt": prompt.strip(), "response": response.strip()}
        return None
    except Exception as exc:
        tqdm.write(f"Projection failed for {file_path}: {exc}")
        return None

def build_output_path(date_str: str, shard_id: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    filename = f"shard{shard_id}-{ts}.jsonl"
    return f"batches/public-merged/{date_str}/{filename}"

def hf_upload(
    client: httpx.Client,
    repo: str,
    file_path: str,
    content: str,
    token: str,
    retries: int = MAX_RETRIES,
) -> bool:
    """
    Commit single file to HF dataset repo via PUT /repos/{repo}/contents/{path}.
    Uses raw HF API (not datasets library) to avoid rate limits during training.
    """
    url = f"https://huggingface.co/api/repos/{repo}/contents/{file_path}"
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "message": f"Add {file_path}",
        "content": base64.b64encode(content.encode()).decode(),
    }

    for attempt in range(1, retries + 1):
        try:
            resp = client.put(url, headers=headers, json=body, timeout=60.0)
            if resp.status_code in (200, 201):
                return True
            # 429 commit cap / rate limit
            if resp.status_code == 429:
                wait = RETRY_WAIT * (BACKOFF_FACTOR ** (attempt - 1))
                tqdm.write(f"HF commit 429 for {file_path}, retry {attempt}/{retries} in {wait}s")
                time.sleep(wait)
                continue
            tqdm.write(f"HF commit failed {resp
