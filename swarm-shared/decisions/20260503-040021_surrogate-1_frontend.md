# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single source of truth** for file listing:
  - Prefer a committed `file-list.json` in the repo for the requested `DATE_FOLDER`.
  - If absent, fall back to **one API call** via `list_repo_tree(path, recursive=False)` and save to `manifest-{DATE_FOLDER}.json` (do not commit).
- Deterministically assigns files to shards by `hash(slug) % SHARD_TOTAL` (consistent across runs).
- Downloads **only via CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header and no additional API calls during ingestion.
- Projects each file to `{prompt, response}` at parse time (avoids mixed-schema pyarrow errors).
- Deduplicates via the existing `lib/dedup.py` (central md5 store) and streams output to:
  ```
  batches/public-merged/<DATE_FOLDER>/shard<SHARD_ID>-<HHMMSS>.jsonl
  ```
- Commits outputs via `huggingface_hub` (HF_TOKEN with write permission).
- Runner remains a Bash wrapper (`#!/usr/bin/env bash`) that calls the Python script to preserve the existing interface.

---

### Steps (timeboxed)

1. Create `bin/dataset-enrich.py` (core worker) — 60 min  
2. Replace `bin/dataset-enrich.sh` with a thin Bash wrapper that calls the Python script (preserve CLI/env interface) — 10 min  
3. Add/confirm `requirements.txt` (`requests`, `huggingface_hub`) — 5 min  
4. Quick smoke test locally (dry-run with mock data) — 15 min  
5. Validate GitHub Actions matrix still works (no functional change to inputs) — 10 min  

Total: ~100 min (safe within 2h).

---

## Code

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1 public dataset.

Environment:
  SHARD_ID (int, required): 0..15
  SHARD_TOTAL (int, default=16)
  DATE_FOLDER (str, default=YYYY-MM-DD today)
  HF_TOKEN (str, required for upload)
  REPO_ID (str, default=axentx/surrogate-1-training-pairs)
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, login

# ---------- config ----------
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
try:
    SHARD_ID = int(os.getenv("SHARD_ID"))
except Exception:
    print("ERROR: SHARD_ID (int) is required", file=sys.stderr)
    sys.exit(1)

DATE_FOLDER = os.getenv("DATE_FOLDER")
if not DATE_FOLDER:
    DATE_FOLDER = datetime.datetime.utcnow().strftime("%Y-%m-%d")

HF_TOKEN = os.getenv("HF_TOKEN")
if HF_TOKEN:
    login(token=HF_TOKEN)
API = HfApi()

# ---------- constants ----------
BASE_RAW_PATH = f"batches/public-raw/{DATE_FOLDER}"
MANIFEST_FALLBACK_PATH = Path(f"manifest-{DATE_FOLDER}.json")
FILE_LIST_COMMITTED = Path(f"batches/public-raw/{DATE_FOLDER}/file-list.json")
OUTPUT_DIR = Path(f"batches/public-merged/{DATE_FOLDER}")
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ---------- helpers ----------
def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % total

def slug_from_path(filepath: str) -> str:
    return Path(filepath).stem

def list_files(date_folder: str) -> List[str]:
    """
    Single source of truth:
    1) committed file-list.json in the date folder if present
    2) else one API call to list_repo_tree (non-recursive) and save to manifest-{date}.json
    """
    # 1) committed file-list.json
    if FILE_LIST_COMMITTED.is_file():
        try:
            data = json.loads(FILE_LIST_COMMITTED.read_text())
            if isinstance(data, list):
                return sorted(data)
            if isinstance(data, dict) and "files" in data:
                return sorted(data["files"])
        except Exception:
            pass

    # 2) fallback: single API call
    try:
        tree = API.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    except Exception as exc:
        print(f"ERROR listing repo tree for {date_folder}: {exc}", file=sys.stderr)
        sys.exit(1)

    paths = [item.path for item in tree if getattr(item, "path", None)]
    files = sorted(p for p in paths if "." in Path(p).name)

    # save local manifest for reproducibility (do not commit)
    MANIFEST_FALLBACK_PATH.write_text(json.dumps({"date_folder": date_folder, "files": files}, indent=2))
    return files

def cdn_url(filepath: str) -> str:
    """CDN bypass URL (no auth)."""
    return f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{filepath}"

def safe_download(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def project_to_pair(raw_bytes: bytes) -> Dict[str, str]:
    """
    Project arbitrary file to {prompt, response} at parse time.
    """
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return {"prompt": "", "response": ""}

    # Try JSON object
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            return {"prompt": str(prompt), "response": str(response)}
    except Exception:
        pass

    # Try JSON lines (first line only)
    lines = text.splitlines()
    if lines:
        try:
            obj = json.loads(lines[0])
            if isinstance(obj, dict):
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
                response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
                return {"prompt": str(prompt), "response": str(response)}
        except Exception:
            pass

    # Fallback: split by common separator
    sep_candidates = ["\nResponse:", "\nAnswer:", "\n---", "\n\n"]
    for sep in sep_candidates:
        if sep in text:
            parts = text.split(sep, 1)
            return {"prompt": parts[0].strip(), "response": parts[1].strip() if len(parts) > 1 else ""}

    # Last resort: 70/30 split
    pivot = max(1, int(len(text) * 0.7))
    return {"prompt": text[:pivot].strip(), "response": text[pivot:].strip()}

def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

# ---------- dedup ----------
def dedup_and_store(raw_hash: str, pair: Dict[str, str]) -> bool:
    """
    Returns True if pair is new (not duplicate), False if duplicate.
    Delegates to lib/dedup.py when available.
    """
    dedup_path = Path(__file__).parent / "lib" / "dedup.py"

