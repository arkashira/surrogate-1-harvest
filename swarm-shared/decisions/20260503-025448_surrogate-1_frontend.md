# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list saved to `manifest-{DATE}.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (avoids 429 API limits)
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Outputs: `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Reuses existing Lightning/knowledge-rag patterns: single API call from orchestrator, CDN-only during training
- **Clean exit if DATE folder empty** (no error)

---

## Code Changes

### 1. New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage:
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py

Environment:
  HF_TOKEN          - HuggingFace write token (for dedup store + upload)
  SHARD_ID          - 0..15
  SHARD_TOTAL       - default 16
  DATE              - folder in dataset repo, e.g. 2026-04-29
  DATASET_REPO      - default axentx/surrogate-1-training-pairs
  OUTPUT_DIR        - default batches/public-merged
"""
import os
import sys
import json
import hashlib
import datetime
import logging
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-enrich")

# ---- config ----
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "batches/public-merged"))

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)
if not DATE:
    log.error("DATE is required (YYYY-MM-DD)")
    sys.exit(1)

API = HfApi(token=HF_TOKEN)

# ---- dedup store ----
from lib.dedup import DedupStore  # type: ignore

dedup = DedupStore()

# ---- helpers ----
def deterministic_shard(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: list top-level files in DATE folder."""
    log.info("Listing repo tree for %s/%s", DATASET_REPO, date_folder)
    tree = API.list_repo_tree(repo_id=DATASET_REPO, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    log.info("Found %d files", len(files))
    return files

def save_manifest(date_folder: str, files: List[str]) -> Path:
    out = Path(f"manifest-{date_folder}.json")
    out.write_text(json.dumps({"date": date_folder, "files": files}, indent=2))
    log.info("Manifest saved to %s", out)
    return out

def cdn_download(repo: str, repo_path: str) -> bytes:
    """
    Download via HF CDN (no Authorization header).
    Bypasses /api/ rate limits.
    """
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{repo_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_to_pair(content: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project to {prompt, response} only at parse time.
    Supports .jsonl and .parquet (via pyarrow if available).
    """
    name = filename.lower()
    pairs = []

    if name.endswith(".jsonl"):
        for line in content.decode().strip().splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            if prompt and response:
                pairs.append({"prompt": prompt, "response": response})
        return pairs

    if name.endswith(".parquet"):
        try:
            import io
            import pyarrow.parquet as pq
            import pandas as pd
            table = pq.read_table(io.BytesIO(content))
            df = table.to_pandas()
            # heuristic columns
            prompt_col = next((c for c in df.columns if "prompt" in c.lower()), None)
            response_col = next((c for c in df.columns if "response" in c.lower()), None)
            if prompt_col is None or response_col is None:
                # fallback: first text col / second text col
                text_cols = [c for c in df.columns if df[c].dtype == "object"]
                if len(text_cols) >= 2:
                    prompt_col, response_col = text_cols[0], text_cols[1]
                else:
                    log.warning("No prompt/response columns in %s", filename)
                    return []
            for _, row in df.iterrows():
                prompt = str(row[prompt_col]) if pd.notna(row[prompt_col]) else ""
                response = str(row[response_col]) if pd.notna(row[response_col]) else ""
                if prompt and response:
                    pairs.append({"prompt": prompt, "response": response})
            return pairs
        except ImportError:
            log.warning("pyarrow/pandas not available, skipping parquet: %s", filename)
            return []

    log.warning("Unsupported file type: %s", filename)
    return []

# ---- main ----
def main() -> None:
    log.info("Shard %d/%d | DATE=%s", SHARD_ID, SHARD_TOTAL, DATE)

    # 1) list files once (single API call)
    files = list_date_files(DATE)
    if not files:
        log.info("DATE folder empty, exiting cleanly")
        return
    save_manifest(DATE, files)

    # 2) determine shard assignment
    my_files = [f for f in files if deterministic_shard(f) == SHARD_ID]
    log.info("Shard %d assigned %d files", SHARD_ID, len(my_files))

    # 3) process
    timestamp = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = OUTPUT_DIR / DATE / f"shard{SHARD_ID}-{timestamp}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    accepted = 0
    skipped_dup = 0
    failed = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for repo_file in my_files:
            try:
                content = cdn_download(DATASET_REPO, repo_file)
                pairs = parse_to_pair(content, repo_file)
                for pair in pairs:
                    # dedup by content hash
                    blob = f"{pair['prompt']}\n{pair['response']}".encode()
                    md5 = hashlib.md5(blob).hexdigest()
                    if dedup.exists(md5):
                        skipped_dup += 1
                        continue
                    dedup.add(md5)
                    fout.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    accepted += 
