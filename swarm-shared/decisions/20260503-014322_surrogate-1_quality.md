# surrogate-1 / quality

## Final Implementation Plan — Manifest-Driven CDN-Bypass Ingestion Worker

**Scope (≤2h)**  
Replace `bin/dataset-enrich.sh` with a single Python worker (`bin/dataset-enrich.py`) that:
- Uses a pre-computed manifest (JSON) listing target files for a date folder.
- Downloads only assigned shard files via HF CDN (`resolve/main/...`) **without Authorization** → bypasses `/api/` rate limits and 429s.
- Projects each file to `{prompt, response}` at parse time → avoids mixed-schema `pyarrow` errors.
- Deduplicates via central md5 store (`lib/dedup.py`).
- Writes clean JSONL to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Keeps GitHub Actions matrix (16 shards) unchanged.

---

### 1) File changes

- Replace `bin/dataset-enrich.sh` → `bin/dataset-enrich.py` (Python worker).
- Keep `.github/workflows/ingest.yml` unchanged (matrix `SHARD_ID`, `DATE`, `MANIFEST_PATH`).
- Keep `lib/dedup.py` unchanged.
- Add lightweight `requirements.txt` additions if needed (`requests`, `tqdm`, `pyarrow`).

---

### 2) Worker behavior

- Inputs (via env):
  - `HF_REPO` (e.g., `datasets/owner/repo`)
  - `DATE` (e.g., `2026-05-03`)
  - `SHARD_ID` (0..15)
  - `N_SHARDS` (default 16)
  - `MANIFEST_PATH` (local JSON path or URL)
  - `HF_TOKEN` (optional; only needed for upload)
- Steps:
  1. Load manifest (local file or URL).
  2. Filter entries assigned to this shard: `hash(slug) % N_SHARDS == SHARD_ID`.
  3. For each file:
     - Build CDN URL: `https://huggingface.co/datasets/{HF_REPO}/resolve/main/{DATE}/{file}`
     - Download with streaming, **no Authorization header**.
     - Parse according to detected schema (JSON/JSONL/Parquet) and project to `{prompt, response}`.
     - Compute md5 of canonical content; skip if already in central dedup store.
     - Append accepted pairs to per-shard JSONL.
  4. Upload result to HF dataset repo: `batches/public-merged/{DATE}/shard{SHARD_ID}-{TIMESTAMP}.jsonl`
  5. Exit 0 on success; non-zero on fatal failure (GitHub Actions will retry).

---

### 3) Code — `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven CDN-bypass ingestion worker.
Usage (via env):
  HF_REPO=datasets/owner/repo \
  DATE=2026-05-03 \
  SHARD_ID=0 \
  N_SHARDS=16 \
  MANIFEST_PATH=manifest-2026-05-03.json \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import requests
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

# ---------- config ----------
HF_REPO = (os.getenv("HF_REPO") or "datasets/axentx/surrogate-1-training-pairs").strip()
DATE = (os.getenv("DATE") or "").strip()
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
N_SHARDS = int(os.getenv("N_SHARDS", "16"))
MANIFEST_PATH = (os.getenv("MANIFEST_PATH") or "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
OUT_DIR = Path(os.getenv("OUT_DIR", "output"))
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
OUT_NAME = f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

if not DATE:
    print("ERROR: DATE env required (YYYY-MM-DD)", file=sys.stderr)
    sys.exit(1)
if not MANIFEST_PATH:
    print("ERROR: MANIFEST_PATH env required", file=sys.stderr)
    sys.exit(1)

OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = OUT_DIR / OUT_NAME

# ---------- helpers ----------
def normalize_repo(repo: str) -> str:
    repo = repo.strip().removeprefix("datasets/").strip()
    return repo

def hf_cdn_url(repo: str, rel_path: str) -> str:
    repo = normalize_repo(repo)
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{rel_path}"

def hf_api_url(repo: str, rel_path: str) -> str:
    repo = normalize_repo(repo)
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{rel_path}"

def hash_slug(path: str) -> int:
    return int(hashlib.sha256(path.encode()).hexdigest(), 16)

def assign_shard(path: str, n: int) -> int:
    return hash_slug(path) % n

def download_stream(url: str, headers: Dict[str, str] = None, timeout: int = 60) -> Iterable[bytes]:
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

def parse_file_to_pairs(content_bytes: bytes, filename: str) -> List[Tuple[str, str]]:
    """
    Parse heterogeneous HF dataset files and project to (prompt, response).
    Supports: JSONL, JSON, Parquet.
    """
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".parquet":
            table = pq.read_table(pa.BufferReader(content_bytes))
            df = table.to_pandas()
        elif suffix == ".jsonl":
            df = pa.json.read_json(pa.BufferReader(content_bytes)).to_pandas()
        elif suffix == ".json":
            df = pa.json.read_json(pa.BufferReader(content_bytes)).to_pandas()
        else:
            # fallback: try JSON/JSONL parse
            try:
                df = pa.json.read_json(pa.BufferReader(content_bytes)).to_pandas()
            except Exception:
                raise ValueError(f"Unsupported file type: {filename}")

        # Normalize column names (case-insensitive)
        cols = {c.lower(): c for c in df.columns}
        prompt_col = None
        response_col = None
        for c in cols:
            if "prompt" in c:
                prompt_col = cols[c]
            if "response" in c or "completion" in c or "answer" in c:
                response_col = cols[c]

        if prompt_col is None or response_col is None:
            # try common fallback columns
            if "text" in cols:
                # Some HF datasets store prompt+response in single text col with separator.
                # We skip these ambiguous cases to avoid garbage; require explicit projection.
                raise ValueError(f"Cannot project {filename}: missing prompt/response columns")
            raise ValueError(f"Cannot project {filename}: missing prompt/response columns")

        pairs = []
        for _, row in df.iterrows():
            p = str(row[prompt_col]).strip()
            r = str(row[response_col]).strip()
            if p and r:
                pairs.append((p, r))
        return pairs
    except Exception as e:
        raise ValueError(f"Failed to parse {filename}: {e}")

def upload_to_hf(local_path: Path, repo: str, repo_path: str, token: str) -> None:
    url = f"https://huggingface.co/api/datasets/{repo}/upload"
    headers = {"Authorization": f"Bearer {token}"}
    with open(local_path, "rb") as f:
        files = {"files": (repo_path, f, "application/jsonl")
