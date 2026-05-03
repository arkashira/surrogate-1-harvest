# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single API call** from the runner (after rate-limit window) to fetch `list_repo_tree(path=DATE_FOLDER, recursive=False)` and saves `file-list.json`.
- Embeds the file list; worker performs **CDN-only fetches** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to bypass `/api/` rate limits.
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas).
- Deduplicates via central md5 store (`lib/dedup.py`) and writes to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.
- Adds retry/backoff for 429 (wait 360s) and 5xx with exponential backoff.
- Keeps the GitHub Actions matrix (16 shards) unchanged; only the worker script is replaced.

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
Env:
  HF_TOKEN          - write token (for dedup store push + final upload)
  DATE_FOLDER       - e.g. 2026-05-03 (default: today)
  MANIFEST_PATH     - optional path to file-list.json (if pre-generated)
"""
import os
import sys
import json
import time
import hashlib
import datetime
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Set

import requests
import pyarrow.parquet as pq
from huggingface_hub import HfApi

# ── config ────────────────────────────────────────────────────────────────
REPO_DATASET = "axentx/surrogate-1-training-pairs"
CDN_BASE = f"https://huggingface.co/datasets/{REPO_DATASET}/resolve/main"
API = HfApi()

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

# ── paths ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DEDUP_PY = BASE_DIR / "lib" / "dedup.py"
OUTPUT_DIR = BASE_DIR / "batches" / "public-merged" / DATE_FOLDER
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── helpers ───────────────────────────────────────────────────────────────
def backoff(attempt: int, base: float = 1.0, cap: float = 360.0) -> float:
    return min(cap, base * (2 ** attempt))

def fetch_file_list() -> List[str]:
    """Single API call to list files in DATE_FOLDER (non-recursive)."""
    if MANIFEST_PATH and Path(MANIFEST_PATH).exists():
        with open(MANIFEST_PATH) as f:
            return json.load(f)

    for attempt in range(6):
        try:
            items = API.list_repo_tree(
                repo_id=REPO_DATASET,
                path=DATE_FOLDER,
                repo_type="dataset",
                token=HF_TOKEN,
                recursive=False,
            )
            # items can be dict or list depending on hf_hub version; normalize
            if isinstance(items, dict) and "entries" in items:
                entries = items["entries"]
            elif isinstance(items, list):
                entries = items
            else:
                entries = []
            files = [e["path"] for e in entries if e.get("type") == "file"]
            # save for reuse in this run
            p = Path(MANIFEST_PATH or (OUTPUT_DIR / "file-list.json"))
            p.write_text(json.dumps(files, indent=2))
            return files
        except Exception as exc:
            wait = backoff(attempt)
            print(f"list_repo_tree failed (attempt {attempt}): {exc} -> wait {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("Failed to list repo tree after retries")

def assign_shard(path: str) -> int:
    """Deterministic shard assignment by slug hash."""
    slug = Path(path).stem
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return h % SHARD_TOTAL

def cdn_url(path: str) -> str:
    return f"{CDN_BASE}/{path}"

def parse_to_pair(local_path: Path) -> List[Dict[str, str]]:
    """Project arbitrary file to {prompt, response} only."""
    suffix = local_path.suffix.lower()
    pairs = []
    try:
        if suffix == ".parquet":
            tbl = pq.read_table(local_path, columns=["prompt", "response"])
            df = tbl.to_pandas()
            for _, row in df.iterrows():
                p = str(row.get("prompt", ""))
                r = str(row.get("response", ""))
                if p and r:
                    pairs.append({"prompt": p, "response": r})
        elif suffix == ".jsonl":
            with open(local_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    p = str(obj.get("prompt", ""))
                    r = str(obj.get("response", ""))
                    if p and r:
                        pairs.append({"prompt": p, "response": r})
        else:
            # fallback: try json
            with open(local_path) as f:
                obj = json.load(f)
                if isinstance(obj, list):
                    for item in obj:
                        p = str(item.get("prompt", ""))
                        r = str(item.get("response", ""))
                        if p and r:
                            pairs.append({"prompt": p, "response": r})
                else:
                    p = str(obj.get("prompt", ""))
                    r = str(obj.get("response", ""))
                    if p and r:
                        pairs.append({"prompt": p, "response": r})
    except Exception as exc:
        print(f"Parse failed {local_path}: {exc}", file=sys.stderr)
    return pairs

def import_dedup() -> Any:
    """Import central dedup module dynamically."""
    if not DEDUP_PY.exists():
        raise FileNotFoundError(f"dedup module not found: {DEDUP_PY}")
    import importlib.util
    spec = importlib.util.spec_from_file_location("dedup", DEDUP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── main ─────────────────────────────────────────────────────────────────
def main() -> None:
    files = fetch_file_list()
    my_files = [f for f in files if assign_shard(f) == SHARD_ID]
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} -> {len(my_files)} files")

    dedup = import_dedup()
    # dedup module expected to expose: mark_seen_and_exists(hashes) -> set[seen_hashes]
    # and possibly: get_db_path()

    written = 0
    total_pairs = 0
    seen_global: Set[str] = set()

    with OUT_FILE.open("w") as out_f:
        for idx, rel_path in enumerate(my_files):
            # CDN bypass: no auth header
            url = cdn_url(rel_path)
            temp = Path("tmp") / Path(rel_path).name
            temp.parent.mkdir(exist_ok
