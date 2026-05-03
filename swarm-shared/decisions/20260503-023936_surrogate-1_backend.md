# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses **one** `list_repo_tree` call (per date folder) to enumerate files, saves manifest JSON, then performs **CDN-only fetches** (`https://huggingface.co/datasets/.../resolve/main/...`) to bypass API rate limits during data loading
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids `pyarrow.CastError`)
- Deduplicates via centralized md5 store (`lib/dedup.py`)
- Writes output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Includes retry/backoff for 429 (wait 360s) and commit-cap mitigation (hash slug → deterministic sibling repo if needed)
- Adds proper Bash shebang and executable bit so cron/workflow invocation is safe

---

### 1) Create new worker (`bin/dataset-enrich.py`)

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage:
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py
"""

import os
import sys
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from huggingface_hub import HfApi, hf_hub_download, login

# --
# Config
# --
REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
OUTPUT_ROOT = Path(__file__).parent.parent / "batches" / "public-merged"
DATE_FMT = "%Y-%m-%d"
TIME_FMT = "%H%M%S"

# Retry/backoff
MAX_RETRIES = 5
RETRY_BACKOFF = [1, 2, 4, 8, 16]
RATE_LIMIT_WAIT = 360  # seconds on 429

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("surrogate-ingest")

# --
# Helpers
# --
def deterministic_shard(key: str, total: int) -> int:
    """Map key to shard by hash."""
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest, "big") % total

def slug_to_repo(key: str) -> str:
    """Spread commits across sibling repos to avoid HF 128/hr cap."""
    digest = hashlib.sha256(key.encode()).digest()
    idx = int.from_bytes(digest, "big") % 5  # 5 siblings
    return f"axentx/surrogate-1-training-pairs-{idx}"

def hf_api_with_retry(api: HfApi, method: str, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            fn = getattr(api, method)
            return fn(*args, **kwargs)
        except Exception as exc:
            if "429" in str(exc):
                log.warning("HF 429 rate limit — waiting %ds", RATE_LIMIT_WAIT)
                time.sleep(RATE_LIMIT_WAIT)
                continue
            if attempt == MAX_RETRIES - 1:
                raise
            sleep = RETRY_BACKOFF[attempt]
            log.warning("Retry %s/%s after %ss: %s", attempt + 1, MAX_RETRIES, sleep, exc)
            time.sleep(sleep)
    raise RuntimeError("Exhausted retries")

def list_date_files(api: HfApi, date_folder: str) -> List[str]:
    """Single API call to list files in date folder (non-recursive)."""
    log.info("Listing repo tree for %s", date_folder)
    tree = hf_api_with_retry(api, "list_repo_tree", repo_id=REPO_ID, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    log.info("Found %d files in %s", len(files), date_folder)
    return files

def download_cdn(path: str, dest: Path) -> bool:
    """Download via CDN (no auth/rate-limit on CDN tier)."""
    url = f"{BASE_CDN}/{path}"
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(url, timeout=30, stream=True) as r:
                r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                log.error("CDN download failed %s: %s", url, exc)
                return False
            sleep = RETRY_BACKOFF[attempt]
            log.warning("CDN retry %s/%s after %ss: %s", attempt + 1, MAX_RETRIES, sleep, exc)
            time.sleep(sleep)
    return False

def parse_to_pair(local_path: Path) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response}.
    Supports: jsonl, json, parquet (via pyarrow if available), text.
    """
    suffix = local_path.suffix.lower()
    try:
        if suffix == ".jsonl":
            import json as jsonlib
            pairs = []
            with open(local_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = jsonlib.loads(line)
                    pairs.append(normalize_pair(obj))
            # For jsonl shards we yield first valid pair (or merge strategy)
            return pairs[0] if pairs else None

        if suffix == ".json":
            import json as jsonlib
            with open(local_path) as f:
                obj = jsonlib.load(f)
            return normalize_pair(obj)

        if suffix == ".parquet":
            try:
                import pyarrow.parquet as pq
                tbl = pq.read_table(local_path, columns=["prompt", "response"])
                df = tbl.to_pandas()
                for _, row in df.iterrows():
                    pair = normalize_pair({"prompt": row.get("prompt"), "response": row.get("response")})
                    if pair:
                        return pair
                return None
            except ImportError:
                log.warning("pyarrow not available for %s", local_path)
                return None

        # fallback text
        with open(local_path) as f:
            content = f.read().strip()
        # crude split: first block as prompt, remainder as response
        parts = content.split("\n\n", 1)
        if len(parts) == 2:
            return normalize_pair({"prompt": parts[0], "response": parts[1]})
        return normalize_pair({"prompt": "", "response": content})
    except Exception as exc:
        log.warning("Parse failed %s: %s", local_path, exc)
        return None

def normalize_pair(obj: Dict) -> Optional[Dict[str, str]]:
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    prompt = str(prompt).strip()
    response = str(response).strip()
    if not prompt and not response:
        return None
    return {"prompt": prompt, "response": response}

# --
# Dedup (central store via lib/dedup.py)
# --
def init_dedup() -> "DedupStore":
    # Import local dedup module
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from lib.dedup import DedupStore
        store_path = Path(__file__).parent.parent / "dedup-central.sqlite"
        return DedupStore(store_path)
    except Exception as exc:
        log.warning("Could not load lib/dedup.py
