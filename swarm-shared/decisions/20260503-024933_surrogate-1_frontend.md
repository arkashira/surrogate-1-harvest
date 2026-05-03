# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list
- Shard assignment by `hash(slug) % SHARD_TOTAL`
- Downloads via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header** during data fetch (bypasses API rate limits)
- Projects heterogeneous schemas to `{prompt, response}` only at parse time
- Dedup via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/<DATE>/shard<N>-<HHMMSS>.jsonl`
- Exits 0 on success, non-zero on fatal error (GitHub Actions will retry)

### Steps (1h 45m)

1. Create `bin/dataset-enrich.py` (1h 15m)
2. Add `#!/usr/bin/env python3`, `requirements.txt` already satisfied
3. Implement CDN downloader with retry/backoff
4. Implement schema projection helpers (handle common HF dataset variants)
5. Integrate `lib/dedup.py` for cross-run dedup
6. Make executable and test locally (30m)
7. Update `.github/workflows/ingest.yml` to use new script (matrix unchanged) (15m)

---

### Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python3 bin/dataset-enrich.py

Environment:
  SHARD_ID        - integer 0..SHARD_TOTAL-1
  SHARD_TOTAL     - total shards (default 16)
  DATE            - date folder on dataset repo (e.g. 2026-04-29)
  HF_TOKEN        - HuggingFace write token (for dedup store + upload)
  DATASET_REPO    - default: axentx/surrogate-1-training-pairs
  OUTPUT_DIR      - default: batches/public-merged
"""

import os
import sys
import json
import hashlib
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from huggingface_hub import HfApi, hf_hub_download

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("surrogate-ingest")

# Constants
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
CDN_BASE = f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main"
API = HfApi()

# Retry config
MAX_RETRIES = 5
BACKOFF_FACTOR = 2
HTTP_TIMEOUT = 30


def deterministic_shard(key: str, total: int) -> int:
    """Deterministic shard assignment by hash(key) % total."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest, 16) % total


def list_date_files(date_folder: str) -> List[str]:
    """
    Single API call to list files in DATE folder (non-recursive).
    Returns relative paths under the date folder.
    """
    try:
        items = API.list_repo_tree(
            repo_id=DATASET_REPO,
            path=date_folder,
            repo_type="dataset",
            recursive=False,
        )
        paths = []
        for item in items:
            if isinstance(item, dict):
                path = item.get("path", "")
            else:
                path = str(item)
            if path:
                paths.append(path)
        log.info("Listed %d files in %s", len(paths), date_folder)
        return paths
    except Exception as exc:
        log.error("Failed to list repo tree: %s", exc, exc_info=True)
        raise


def download_via_cdn(repo_path: str, dest: Path) -> bool:
    """
    Download via HF CDN (no Authorization header) to bypass API rate limits.
    Returns True on success.
    """
    url = f"{CDN_BASE}/{repo_path}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT, stream=True)
            resp.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            log.debug("Downloaded %s -> %s", repo_path, dest)
            return True
        except Exception as exc:
            wait = BACKOFF_FACTOR ** attempt
            log.warning("Download attempt %d/%d failed for %s: %s — retry in %ss",
                        attempt, MAX_RETRIES, repo_path, exc, wait)
            time.sleep(wait)
    log.error("All retries exhausted for %s", repo_path)
    return False


def project_to_pair(raw_obj: Dict) -> Optional[Tuple[str, str]]:
    """
    Project heterogeneous HF dataset objects to (prompt, response).
    Returns None if object cannot be projected.
    """
    if not isinstance(raw_obj, dict):
        return None

    # Common schema variants observed in public datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "text"}
    response_keys = {"response", "output", "answer", "completion", "text"}

    # If both prompt/response present, prefer them
    if "prompt" in raw_obj and "response" in raw_obj:
        return str(raw_obj["prompt"]), str(raw_obj["response"])

    # Try to find one prompt-like and one response-like key
    prompt_candidates = [k for k in prompt_keys if k in raw_obj]
    response_candidates = [k for k in response_keys if k in raw_obj]

    # Avoid using same key for both unless it's the only key and looks like a pair
    if prompt_candidates and response_candidates:
        # Remove intersection if possible
        common = set(prompt_candidates) & set(response_candidates)
        if common and len(prompt_candidates) > 1 and len(response_candidates) > 1:
            prompt_candidates = [k for k in prompt_candidates if k not in common]
            response_candidates = [k for k in response_candidates if k not in common]

        if prompt_candidates and response_candidates:
            pk = prompt_candidates[0]
            rk = response_candidates[0]
            return str(raw_obj[pk]), str(raw_obj[rk])

    # Fallback: if exactly two keys, treat first as prompt, second as response
    keys = [k for k in raw_obj.keys() if isinstance(raw_obj[k], (str, int, float, bool))]
    if len(keys) == 2:
        return str(raw_obj[keys[0]]), str(raw_obj[keys[1]])

    # Last resort: if single text key, split by common separators
    if "text" in raw_obj:
        text = str(raw_obj["text"]).strip()
        # Try to split by ### or <|im_start|> or double newline
        import re
        parts = re.split(r"\n###|\n<\|im_start\|>|\n\n", text)
        if len(parts) >= 2:
            return parts[0].strip(), parts[1].strip()

    return None


def load_dedup_store() -> "DedupStore":
    from lib.dedup import DedupStore
    return DedupStore()


def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date_folder = os.getenv("DATE", "").strip()
    hf_token = os.getenv("HF_TOKEN", "")
   
