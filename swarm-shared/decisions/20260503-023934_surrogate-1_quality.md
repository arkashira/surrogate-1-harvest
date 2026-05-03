# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree` call from Mac (outside training) → `file-list-<DATE>.json` committed to repo (or passed via workflow artifact). Worker loads this manifest and processes only its deterministic shard.
- Uses **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) for all file downloads — zero API calls during ingestion, avoids 429/128-commit limits.
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (no `load_dataset(streaming=True)` on mixed schemas).
- Deduplicates via central `lib/dedup.py` md5 store (unchanged).
- Writes normalized JSONL to `batches/public-merged/<DATE>/shard<N>-<HHMMSS>.jsonl` and streams upload via `huggingface_hub` multipart commit (one commit per shard).
- Adds retry/backoff for CDN flakiness and validates HF token scope before starting.

### Steps (1h 30m total)

1. **Create `bin/dataset-enrich.py`** (45m) — manifest loader, CDN downloader, schema projector, shard selector, dedup, upload.
2. **Update `.github/workflows/ingest.yml`** (15m) — add `DATE` input, pass `SHARD_ID` matrix, generate file-list artifact on schedule (or reuse cached list), set `SHELL=/bin/bash`.
3. **Add `bin/generate-manifest.py`** (15m) — one-off script for Mac to produce `file-list-<DATE>.json` from `list_repo_tree` per folder (non-recursive pagination) and commit to repo or upload as workflow artifact.
4. **Smoke test** (15m) — run one shard locally with a test token, verify JSONL shape and upload.

---

## bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py

Environment:
  HF_TOKEN            - HuggingFace write token (must have repo push)
  SHARD_ID            - 0..SHARD_TOTAL-1
  SHARD_TOTAL         - default 16
  DATE                - date folder to process (e.g. 2026-04-29)
  DATASET_REPO        - default axentx/surrogate-1-training-pairs
  MANIFEST_PATH       - path to file-list JSON; if not provided, uses
                        file-list-{DATE}.json in repo root
"""

import json
import os
import sys
import hashlib
import time
import random
from pathlib import Path
from typing import Dict, List, Any, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download

# -----------------------------
# Configuration
# -----------------------------
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE")
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
MANIFEST_PATH = os.getenv("MANIFEST_PATH") or f"file-list-{DATE}.json"

if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)
if not DATE:
    print("ERROR: DATE required (YYYY-MM-DD)", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)

# -----------------------------
# Dedup (central md5 store)
# -----------------------------
DEDUP_DB_PATH = Path(__file__).parent / "lib" / "dedup.py"
if DEDUP_DB_PATH.exists():
    # Import local dedup module if available
    import importlib.util
    spec = importlib.util.spec_from_file_location("dedup", str(DEDUP_DB_PATH))
    dedup_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dedup_mod)
    is_duplicate = getattr(dedup_mod, "is_duplicate", None)
    mark_seen = getattr(dedup_mod, "mark_seen", None)
else:
    # Fallback simple in-memory dedup for worker isolation (cross-run dedup
    # relies on central store on HF Space)
    _seen = set()
    def is_duplicate(key: str) -> bool:
        return key in _seen
    def mark_seen(key: str) -> None:
        _seen.add(key)

# -----------------------------
# Helpers
# -----------------------------
def deterministic_shard(key: str, total: int) -> int:
    """Map key to shard by md5."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest, 16) % total

def load_manifest(path: str) -> List[str]:
    """Load file list manifest (relative repo paths)."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "files" in data:
            return data["files"]
        if isinstance(data, list):
            return data
        raise ValueError(f"Unexpected manifest format: {path}")
    # If manifest missing, attempt to fetch from repo root via CDN info (best-effort)
    raise FileNotFoundError(f"Manifest not found: {path}")

def cdn_download(repo: str, repo_path: str, timeout: int = 30) -> bytes:
    """Download via HF CDN (no Authorization header -> bypass API rate limits)."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{repo_path}"
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            if resp.status_code == 200:
                return resp.content
            # 404/403 -> skip or raise
            if resp.status_code == 404:
                raise FileNotFoundError(f"CDN 404: {repo_path}")
            # retry on 5xx/429-like CDN tiers
            wait = (2 ** attempt) + random.uniform(0, 1)
            print(f"CDN {resp.status_code} for {repo_path}, retry in {wait:.1f}s")
            time.sleep(wait)
        except requests.RequestException as e:
            wait = (2 ** attempt) + random.uniform(0, 1)
            print(f"CDN error {e} for {repo_path}, retry in {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {repo_path} after retries")

# Minimal schema projector for common surrogate-1 file types.
# Extend as needed per observed schemas.
def project_to_pair(raw: Dict[str, Any], filename: str) -> Optional[Dict[str, str]]:
    """Return {prompt, response} or None if unprojectable."""
    # JSON/JSONL rows
    if isinstance(raw, dict):
        # Common patterns
        if "prompt" in raw and "response" in raw:
            return {"prompt": str(raw["prompt"]), "response": str(raw["response"])}
        if "input" in raw and "output" in raw:
            return {"prompt": str(raw["input"]), "response": str(raw["output"])}
        if "question" in raw and "answer" in raw:
            return {"prompt": str(raw["question"]), "response": str(raw["answer"])}
        # Single-field fallback: treat one field as prompt, another as response
        keys = list(raw.keys())
        if len(keys) == 2:
            return {"prompt": str(raw[keys[0]]), "response": str(raw[keys[1]])}
        # If only one text-like field, duplicate (or skip)
        for k in ("text", "content", "message"):
            if k in raw:
                return {"prompt": "", "response": str(raw[k])}
        return None
    # If raw
