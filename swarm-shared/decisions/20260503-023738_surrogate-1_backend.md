# surrogate-1 / backend

Here is the consolidated, corrected, and fully actionable final version.  
It merges the strongest points from both proposals (manifest-driven, CDN-bypass, deterministic sharding, schema-tolerant projection, central dedup, GitHub Actions integration) and resolves ambiguities in favor of correctness and operational safety.

---

## Final Implementation Plan (≤2 h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Performs **one** `list_repo_tree` call (per date) to enumerate parquet files and saves a local manifest JSON for reproducibility and retries
- Downloads **only via CDN** (no `Authorization` header) to bypass HF API rate limits
- Projects each parquet to `{prompt, response}` at parse time with robust schema fallback to avoid `pyarrow.CastError` on mixed schemas
- Deduplicates via central md5 store (`lib/dedup.py`) using `(prompt, response)` hash
- Writes deterministic shard output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Returns deterministic exit codes for GitHub Actions matrix jobs

Estimated steps:
1. Create `bin/dataset-enrich.py` (45 min) — manifest, CDN download, projection, dedup, output
2. Update `.github/workflows/ingest.yml` (15 min) — switch to Python worker and matrix env
3. Update `requirements.txt` (5 min) — ensure `requests`, `pyarrow`, `huggingface_hub`
4. Remove `bin/dataset-enrich.sh` (5 min)
5. Smoke test locally (20 min) — dry-run with mocked CDN and fake token

---

## Final Code

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Surrogate-1 CDN-bypass ingestion worker.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 HF_TOKEN=hf_xxx \
    python bin/dataset-enrich.py

Behavior:
- One list_repo_tree call for the date folder (or fallback CDN discovery)
- Saves file manifest to manifest-{DATE}-shard{SHARD_ID}.json
- Downloads via CDN (no auth header) to bypass HF API rate limits
- Projects each parquet to {prompt, response} at parse time
- Deduplicates via lib.dedup by md5 of (prompt, response)
- Writes shard output to batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
- Deterministic exit codes for GitHub Actions matrix
"""

import os
import sys
import json
import hashlib
import datetime
import concurrent.futures
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
import pyarrow.parquet as pq
import pyarrow as pa

# HF Hub for listing (optional at runtime; fallback to CDN discovery)
try:
    from huggingface_hub import HfApi
    HF_AVAILABLE = True
except Exception:
    HF_AVAILABLE = False

REPO_DATASET = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_DATASET}/resolve/main"

# Central dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from lib.dedup import DedupStore  # type: ignore
except Exception as e:
    print(f"[ERROR] Cannot import lib.dedup: {e}", file=sys.stderr)
    sys.exit(2)

# ----------
# Helpers
# ----------

def _now_tag() -> str:
    return datetime.datetime.utcnow().strftime("%H%M%S")

def _hash_pair(prompt: str, response: str) -> str:
    return hashlib.md5((prompt + "\0" + response).encode("utf-8")).hexdigest()

def _normalize_text(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()

def shard_filter(items: List[str], shard_id: int, shard_total: int) -> List[str]:
    """Deterministic shard assignment by hash of item path."""
    assigned = []
    for item in items:
        h = int(hashlib.md5(item.encode("utf-8")).hexdigest(), 16)
        if h % shard_total == shard_id:
            assigned.append(item)
    assigned.sort()
    return assigned

def list_date_files(date: str, hf_token: Optional[str]) -> List[str]:
    """
    Single API call to list parquet files under date folder.
    Falls back to CDN discovery if HF API unavailable/rate-limited.
    Returns paths relative to repo root (e.g. 'batches/public/2026-04-29/file.parquet').
    """
    candidates = _try_list_via_hf_api(date, hf_token)
    if candidates:
        return candidates
    return _list_date_files_cdn_fallback(date)

def _try_list_via_hf_api(date: str, hf_token: Optional[str]) -> List[str]:
    if not HF_AVAILABLE:
        return []
    try:
        api = HfApi(token=hf_token)
        prefixes_to_try = [
            date,
            f"batches/{date}",
            f"batches/public/{date}",
            f"batches/public-merged/{date}",
            f"enriched/{date}",
        ]
        for prefix in prefixes_to_try:
            try:
                tree = api.list_repo_tree(
                    repo_id=REPO_DATASET,
                    path=prefix,
                    recursive=False,
                    repo_type="dataset",
                )
                files = [item.rfilename for item in tree if item.rfilename.endswith(".parquet")]
                if files:
                    # Return full relative paths
                    return files
            except Exception:
                continue
        return []
    except Exception as e:
        print(f"[WARN] HF list_repo_tree failed: {e}; falling back to CDN discovery", file=sys.stderr)
        return []

def _list_date_files_cdn_fallback(date: str) -> List[str]:
    """
    Conservative CDN discovery: try known prefixes and parse directory-like HTML.
    Prefer explicit manifest or API listing in production; this is fallback only.
    """
    prefixes = [
        f"{date}",
        f"batches/{date}",
        f"batches/public/{date}",
        f"batches/public-merged/{date}",
        f"enriched/{date}",
    ]
    found = []
    for prefix in prefixes:
        url = f"{BASE_CDN}/{prefix}/"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                for m in re.finditer(r'href="([^"]+\.parquet)"', r.text):
                    rel = f"{prefix}/{m.group(1)}"
                    # Normalize double slashes
                    rel = re.sub(r"/+", "/", rel)
                    found.append(rel)
            if found:
                return found
        except Exception:
            continue
    return found

def download_parquet_cdn(path: str, timeout: int = 30) -> bytes:
    """CDN download without Authorization header."""
    url = f"{BASE_CDN}/{path}"
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

def project_to_pair(content: bytes, path: str) -> List[Dict[str, str]]:
    """
    Project parquet bytes to {prompt, response} pairs.
    Tolerates mixed schemas by selecting columns heuristically.
    """
    pairs = []
    try:
        table = pq.read_table(pa.BufferReader(content))
    except Exception as e:
        print(f"[WARN] Failed to read parquet {path}: {e}", file=sys.stderr)
        return pairs

    # Normalize column names
    cols = {c.strip().lower(): c for c in table.column_names}

    # Heuristic mapping
    prompt_col = None
    response_col = None

    for key, variants in [
        ("prompt", ["prompt", "question", "input", "instruction", "user"]),
        ("response", ["response", "answer", "output", "completion", "assistant"]),
    ]:
        for v in variants:
            if v in cols:
                if key == "prompt" and prompt_col is None:
                    prompt_col
