# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env (required)
- Single API call: `list_repo_tree(recursive=False)` on `batches/public-raw/{DATE}/` → saves `manifest-{DATE}.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads **only assigned files** via HF CDN (`resolve/main/...`) — zero API/auth calls during data load, bypasses 429 limits
- Projects heterogeneous schemas → `{prompt, response}` only at parse time (avoids pyarrow CastError); drops `source`, `ts`, extra fields
- Dedups via centralized `lib/dedup.py` md5 store (file-backed)
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Includes retry/backoff for CDN downloads; commit-cap handling is inherent via sharding

---

### 1. Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (via env):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""
import os
import sys
import json
import time
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi

# --
# Config
# --
REPO_ID = "axentx/surrogate-1-training-pairs"
RAW_PREFIX = "batches/public-raw"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# Dedup store (file-backed)
DEDUP_DB = Path("lib/dedup.py")
if DEDUP_DB.exists():
    sys.path.insert(0, str(DEDUP_DB.parent))
    try:
        from dedup import is_duplicate, add_hash
    except Exception:
        logging.warning("dedup import failed, using in-memory set")
        _seen = set()
        def is_duplicate(h): return h in _seen
        def add_hash(h): _seen.add(h)
else:
    _seen = set()
    def is_duplicate(h): return h in _seen
    def add_hash(h): _seen.add(h)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("enrich")

# --
# Helpers
# --
def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def api() -> HfApi:
    return HfApi(token=HF_TOKEN)

def fetch_manifest(date: str) -> List[str]:
    """Single API call to list files in date folder; cache locally."""
    manifest_path = Path(f"manifest-{date}.json")
    if manifest_path.exists():
        log.info("Using cached manifest %s", manifest_path)
        return json.loads(manifest_path.read_text())

    prefix = f"{RAW_PREFIX}/{date}"
    log.info("Fetching repo tree for %s (prefix=%s)", date, prefix)
    items = api().list_repo_tree(repo_id=REPO_ID, path=prefix, recursive=False)
    # Keep only files (ignore subfolders). Expecting raw parquet/jsonl/etc.
    files = [it.rfilename for it in items if it.type == "file"]
    manifest_path.write_text(json.dumps(files, indent=2))
    log.info("Saved manifest with %d files", len(files))
    return files

def cdn_download(url: str, dest: Path, max_retries: int = 5) -> bool:
    headers = {"User-Agent": "axentx-surrogate-1/1.0"}
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as exc:
            wait = 2 ** attempt
            log.warning("CDN download failed (%s/%s): %s — retry in %ss", attempt, max_retries, exc, wait)
            time.sleep(wait)
    return False

def parse_file_to_pairs(local_path: Path) -> List[Dict[str, str]]:
    """
    Schema-aware projection to {prompt, response} only.
    Supports JSONL and parquet via pyarrow (if available).
    Drops source, ts, extra fields.
    """
    pairs = []
    suffix = local_path.suffix.lower()

    if suffix == ".jsonl":
        for line in local_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            if prompt and response:
                pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    # Parquet fallback
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(local_path)
        cols = table.column_names
        prompt_col = next((c for c in ["prompt", "input", "question"] if c in cols), None)
        response_col = next((c for c in ["response", "output", "answer"] if c in cols), None)
        if prompt_col and response_col:
            prompts = table.column(prompt_col).to_pylist()
            responses = table.column(response_col).to_pylist()
            for p, r in zip(prompts, responses):
                if p and r:
                    pairs.append({"prompt": str(p), "response": str(r)})
        return pairs
    except Exception:
        log.warning("Could not parse %s as parquet/jsonl", local_path)
        return []

# --
# Main
# --
def main() -> None:
    files = fetch_manifest(DATE)
    if not files:
        log.warning("No files found for date %s", DATE)
        return

    assigned = [f for f in files if slug_hash(f) % SHARD_TOTAL == SHARD_ID]
    log.info("Shard %d/%d assigned %d files out of %d total", SHARD_ID, SHARD_TOTAL, len(assigned), len(files))

    written = 0
    skipped_dupes = 0
    os.makedirs(OUT_DIR, exist_ok=True)

    with OUT_FILE.open("w", buffering=1) as out_f:
        for rel_path in assigned:
            cdn_url = f"{BASE_CDN}/{rel_path}"
            local_file = Path("tmp") / rel_path.replace("/", "_")
            local_file.parent.mkdir(parents=True, exist_ok=True)

            ok = cdn_download(cdn_url, local_file)
            if not ok:
                log.error("Failed to download %s", rel_path)
                continue

            pairs = parse_file_to_pairs(local_file)
            local_file.unlink(missing_ok=True)

            for pair in pairs:
                text = json.dumps(pair, ensure_ascii=False)
                h = hashlib.md5(text.encode()).hexdigest()
                if is_duplicate(h
