# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Uses **manifest-first strategy**: single `list_repo_tree` call (per date folder) → saves `manifest.json`; worker loads manifest and processes only its shard slice
- **CDN-bypass downloads**: uses `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth header) to bypass `/api/` rate limits
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids pyarrow CastError)
- Dedup via central `lib/dedup.py` md5 store (unchanged)
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with deterministic filename (no collisions)
- Adds retry/backoff for 429 (360s wait) and 5xx (exponential)
- Keeps `requirements.txt` minimal (adds `requests` if not present)

### Steps (≤2h)
1. Create `bin/dataset-enrich.py` (new worker)
2. Update `.github/workflows/ingest.yml` to invoke python worker with matrix env
3. Add `requests` to `requirements.txt`
4. Remove old `bin/dataset-enrich.sh` (or keep as wrapper for backward compat)

---

## Code Snippets

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Environment:
  SHARD_ID (int, required): 0..SHARD_TOTAL-1
  SHARD_TOTAL (int, default=16)
  DATE (str, required): YYYY-MM-DD folder on HF repo
  HF_TOKEN (str, required): write token for uploads
  REPO_ID (str, default=axentx/surrogate-1-training-pairs)
"""

import os
import sys
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from dedup import DedupStore  # noqa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("dataset-enrich")

# Constants
DEFAULT_REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = "https://huggingface.co/datasets"
MAX_RETRIES = 5
RETRY_BACKOFF = [1, 2, 4, 8, 16]
RATE_LIMIT_WAIT = 360  # seconds (per pattern)


def get_env(name: str, default: str = None) -> str:
    val = os.getenv(name, default)
    if val is None:
        log.error("Missing required env: %s", name)
        sys.exit(1)
    return val


def hf_api_with_retry() -> HfApi:
    token = get_env("HF_TOKEN")
    return HfApi(token=token)


def list_date_manifest(api: HfApi, repo: str, date: str) -> List[str]:
    """
    Single API call to list files in date folder (non-recursive).
    Returns list of file paths (relative to repo root).
    """
    for attempt in range(MAX_RETRIES):
        try:
            tree = api.list_repo_tree(repo=repo, path=date, recursive=False)
            files = [item.rfilename for item in tree if item.rfilename]
            log.info("Listed %d files in %s/%s", len(files), repo, date)
            return files
        except Exception as exc:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            if hasattr(exc, "response") and exc.response is not None:
                if exc.response.status_code == 429:
                    log.warning("HF API 429 — waiting %ds", RATE_LIMIT_WAIT)
                    time.sleep(RATE_LIMIT_WAIT)
                    continue
                if exc.response.status_code >= 500:
                    log.warning("HF API %s — retry in %ds", exc.response.status_code, wait)
                    time.sleep(wait)
                    continue
            log.error("Failed to list repo tree (attempt %d): %s", attempt + 1, exc)
            if attempt + 1 >= MAX_RETRIES:
                raise
            time.sleep(wait)
    raise RuntimeError("Exhausted retries listing repo tree")


def download_via_cdn(repo: str, path: str, dest: Path) -> Path:
    """
    Download via CDN (no auth header) to bypass /api/ rate limits.
    """
    url = f"{BASE_CDN}/{repo}/resolve/main/{path}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=30, stream=True)
            if resp.status_code == 429:
                wait = RATE_LIMIT_WAIT
                log.warning("CDN 429 — waiting %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            log.debug("Downloaded %s -> %s", path, dest)
            return dest
        except Exception as exc:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            log.warning("Download failed %s (attempt %d): %s", url, attempt + 1, exc)
            if attempt + 1 >= MAX_RETRIES:
                raise
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {url}")


def project_to_pair(raw_obj: Dict[str, Any], source_path: str) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Heuristic-based; adapt per known schemas.
    """
    # Common patterns observed in surrogate-1 datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "completion", "output", "answer", "assistant"}

    # Try direct keys first
    for pk in prompt_keys:
        if pk in raw_obj and isinstance(raw_obj[pk], str):
            prompt = raw_obj[pk].strip()
            break
    else:
        # Fallback: first string field that looks like prompt
        for k, v in raw_obj.items():
            if isinstance(v, str) and len(v) > 10:
                prompt = v.strip()
                break
        else:
            prompt = json.dumps(raw_obj, ensure_ascii=False)

    for rk in response_keys:
        if rk in raw_obj and isinstance(raw_obj[rk], str):
            response = raw_obj[rk].strip()
            break
    else:
        # Fallback: second string field or empty
        found = False
        for k, v in raw_obj.items():
            if isinstance(v, str) and len(v) > 5 and k not in prompt_keys:
                response = v.strip()
                found = True
                break
        if not found:
            response = ""

    return {"prompt": prompt, "response": response}


def compute_md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def main() -> None:
    shard_id = int(get_env("SHARD_ID"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date = get_env("DATE")  # YYYY-MM-DD
    repo = get_env("REPO_ID", DEFAULT_REPO)

    if not (0 <= shard_id < shard_total):
        log.error("Invalid SHARD_ID=%d for SHARD_TOTAL=%d", shard_id, shard_total)
        sys.exit(1)

    api = hf_api_with_retry()
    dedup = DedupStore()
