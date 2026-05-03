# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single API call** from the runner (after rate-limit window) to list one date folder via `list_repo_tree(recursive=False)` and saves the file list to `manifest.json`.
- Embeds the manifest in the runner; worker performs **CDN-only fetches** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to bypass `/api/` rate limits.
- Streams each assigned file (by shard hash), normalizes heterogeneous schemas to `{prompt, response}`, dedups via central md5 store (`lib/dedup.py`), and writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Adds retry/backoff for 429 on CDN (rare) and commit-cap spreading across sibling repos when pushing results.

---

### 1) New file: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
Optional:
  DATE_FOLDER=2026-05-03
"""
import os
import sys
import json
import hashlib
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, list_repo_tree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-enrich")

# ---- config ----
REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
MANIFEST_PATH = Path("manifest.json")
OUTPUT_DIR = Path("batches/public-merged") / DATE_FOLDER
SLEEP_ON_429 = 360
CDN_BASE = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
SIBLING_REPOS = [
    f"axentx/surrogate-1-training-pairs",
    f"axentx/surrogate-1-training-pairs-s1",
    f"axentx/surrogate-1-training-pairs-s2",
    f"axentx/surrogate-1-training-pairs-s3",
    f"axentx/surrogate-1-training-pairs-s4",
]

# ---- dedup bridge (reuse existing) ----
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import is_duplicate, mark_seen  # type: ignore

# ---- helpers ----
def shard_assign(key: str) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.md5(key.encode()).hexdigest()
    return int(digest, 16) % SHARD_TOTAL

def build_manifest() -> List[str]:
    """Single API call: list files in DATE_FOLDER (non-recursive)."""
    api = HfApi(token=HF_TOKEN)
    try:
        tree = list_repo_tree(repo_id=REPO_ID, path=DATE_FOLDER, recursive=False)
    except Exception as e:
        log.error("Failed to list repo tree: %s", e)
        raise
    files = [item.rfilename for item in tree if item.type == "file"]
    # save for reproducibility / debugging
    MANIFEST_PATH.write_text(json.dumps(files, indent=2))
    log.info("Manifest saved: %d files in %s", len(files), DATE_FOLDER)
    return files

def cdn_download(url: str, timeout: int = 30) -> bytes:
    """CDN-only fetch (no auth header) with retry/backoff."""
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=timeout, headers={})
            if resp.status_code == 429:
                wait = SLEEP_ON_429
                log.warning("CDN 429, waiting %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            if attempt == 4:
                raise
            sleep = (2 ** attempt) + 1
            log.warning("Download failed (%s), retry in %ss", e, sleep)
            time.sleep(sleep)
    raise RuntimeError("Unreachable")

def normalize_record(raw: Dict[str, Any]) -> Dict[str, str]:
    """Project heterogeneous schemas to {prompt, response}."""
    # Common patterns seen in surrogate-1 datasets
    prompt = None
    response = None

    # direct fields
    if "prompt" in raw and isinstance(raw["prompt"], str):
        prompt = raw["prompt"].strip()
    elif "instruction" in raw and isinstance(raw["instruction"], str):
        prompt = raw["instruction"].strip()
    elif "input" in raw and isinstance(raw["input"], str):
        prompt = raw["input"].strip()
    elif "question" in raw and isinstance(raw["question"], str):
        prompt = raw["question"].strip()

    if "response" in raw and isinstance(raw["response"], str):
        response = raw["response"].strip()
    elif "output" in raw and isinstance(raw["output"], str):
        response = raw["output"].strip()
    elif "completion" in raw and isinstance(raw["completion"], str):
        response = raw["completion"].strip()
    elif "answer" in raw and isinstance(raw["answer"], str):
        response = raw["answer"].strip()

    # If still missing, best-effort: pick first/second text-like field
    if prompt is None or response is None:
        text_fields = [v for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(text_fields) >= 2:
            if prompt is None:
                prompt = text_fields[0].strip()
            if response is None:
                response = text_fields[1].strip()

    # Fallbacks
    if prompt is None:
        prompt = ""
    if response is None:
        response = ""
    return {"prompt": prompt, "response": response}

def pick_sibling_repo(slug: str) -> str:
    """Spread commits across sibling repos deterministically."""
    idx = hashlib.md5(slug.encode()).hexdigest()
    idx = int(idx, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def upload_results(lines: List[Dict[str, str]]) -> None:
    """Write JSONL locally and push to HF dataset (spread across siblings)."""
    if not lines:
        log.info("No new lines to upload.")
        return

    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    local_path = OUTPUT_DIR / f"shard{SHARD_ID}-{ts}.jsonl"
    with local_path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    log.info("Wrote %s (%d lines)", local_path, len(lines))

    # Push to dataset repo (primary + siblings if configured)
    api = HfApi(token=HF_TOKEN)
    target_repo = pick_sibling_repo(f"shard{SHARD_ID}-{DATE_FOLDER}")
    remote_path = f"batches/public-merged/{DATE_FOLDER}/shard{SHARD_ID}-{ts}.jsonl"
    try:
        api.upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=remote_path,
            repo_id=target_repo,
            repo_type="dataset",
        )
        log.info("Uploaded to %s:%s", target_repo, remote_path)
    except Exception as e:
        log.error("Upload failed: %s
