# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single API call** from the runner (after rate-limit window) to fetch `list_repo_tree(path=DATE_FOLDER, recursive=False)` and saves the file list to `manifest.json`. Embeds this list in the worker so training/data loads use **CDN-only fetches** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization headers during streaming — bypassing `/api/` rate limits entirely.
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`.
- Per-file streaming with schema projection to `{prompt, response}` only at parse time (avoids `load_dataset(streaming=True)` on heterogeneous repos).
- Central md5 dedup via existing `lib/dedup.py` (SQLite store) to prevent duplicates across shards/runs.
- Outputs to `batches/public-merged/<DATE_FOLDER>/shard<SHARD_ID>-<HHMMSS>.jsonl` with one JSON object per line; no extra `source`/`ts` columns (attribution via filename pattern).
- Exits cleanly on HF 429: wait 360s then retry; uses `list_repo_tree` per folder (non-recursive) to avoid 100× pagination.
- Reusable across cron/GitHub Actions matrix and local dev.

---

### File: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py

Usage (manual):
  python bin/dataset-enrich.py --shard-id 0 --shard-total 16 --date-folder 2026-05-03

Environment:
  HF_TOKEN         - write token for axentx/surrogate-1-training-pairs
  DATASET_REPO     - default: axentx/surrogate-1-training-pairs
  DATE_FOLDER      - default: today YYYY-MM-DD
"""

import json
import hashlib
import os
import sys
import time
import datetime
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# Local dedup module (shared with HF Space)
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from dedup import DedupStore  # type: ignore

# ---------- config ----------
HF_TOKEN = os.getenv("HF_TOKEN")
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
API = HfApi(token=HF_TOKEN)

CDN_BASE = f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main"
BATCHES_DIR = Path("batches/public-merged")

# HF API polite defaults
RETRY_WAIT = 360  # seconds on 429
MAX_RETRIES = 3
TIMEOUT = 30

# ---------- helpers ----------
def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def deterministic_shard(slug: str, shard_total: int) -> int:
    return int(sha256_hex(slug), 16) % shard_total

def now_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

def date_folder_default() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def api_get_with_retry(url: str, headers: Optional[Dict[str, str]] = None, **kwargs) -> requests.Response:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT, **kwargs)
            if resp.status_code == 429:
                wait = RETRY_WAIT
                print(f"[rate-limit] 429, waiting {wait}s (attempt {attempt}/{MAX_RETRIES})", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt
            print(f"[retry] {e}, waiting {wait}s (attempt {attempt}/{MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")

# ---------- manifest ----------
def build_manifest(date_folder: str, output_path: Path) -> List[str]:
    """
    Single API call: list_repo_tree(non-recursive) for date_folder.
    Returns list of file paths (relative to dataset root) and saves manifest.
    """
    print(f"[manifest] listing {DATASET_REPO}/{date_folder} (non-recursive)", file=sys.stderr)
    try:
        tree = list_repo_tree(
            repo_id=DATASET_REPO,
            path=date_folder,
            recursive=False,
            token=HF_TOKEN,
        )
    except Exception as e:
        print(f"[manifest] failed to list tree: {e}", file=sys.stderr)
        raise

    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"date_folder": date_folder, "files": files}, f, indent=2)
    print(f"[manifest] saved {len(files)} files -> {output_path}", file=sys.stderr)
    return files

def load_manifest(manifest_path: Path) -> List[str]:
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    return data["files"]

# ---------- cdn stream + projection ----------
def stream_file_cdn(repo: str, file_path: str) -> bytes:
    url = f"{CDN_BASE}/{file_path}"
    resp = api_get_with_retry(url)
    return resp.content

def project_to_pair(raw_bytes: bytes, file_path: str) -> Optional[Dict[str, str]]:
    """
    Minimal projection to {prompt, response}.
    Supports common surrogate-1 file shapes:
      - JSON/JSONL objects with prompt/response fields (case-insensitive).
      - Parquet handled externally (this worker streams raw bytes and does lightweight decode).
    If projection fails or file is not a pair, returns None.
    """
    import io

    name = file_path.lower()
    try:
        if name.endswith(".json") or name.endswith(".jsonl"):
            # try json lines first
            text = raw_bytes.decode("utf-8")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            pairs = []
            for ln in lines:
                obj = json.loads(ln)
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                response = obj.get("response") or obj.get("output") or obj.get("answer")
                if prompt is not None and response is not None:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
            # return first pair for per-file emission (caller can iterate if needed)
            if pairs:
                return pairs[0]
            return None

        if name.endswith(".parquet"):
            import pyarrow.parquet as pq
            table = pq.read_table(io.BytesIO(raw_bytes))
            df = table.to_pandas()
            # find columns
            prompt_col = next((c for c in df.columns if c.lower() in ("prompt", "input", "question")), None)
            response_col = next((c for c in df.columns if c.lower() in ("response", "output", "answer")), None)
            if prompt_col and response_col:
                for _, row in df.iterrows():
                    return {"prompt": str(row[prompt_col]), "response": str(row[response_col])}
            return None

        # fallback: try decode as json single object
        if name.endswith(".json"):
            obj = json.loads(raw_bytes.decode("
