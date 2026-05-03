# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list saved to `manifest-{DATE}.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API calls during data load, avoids 429/128-commit cap
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Dedup via central `lib/dedup.py` md5 store (same as existing)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns (filename carries attribution)
- Reuses existing HF dataset repo; spreads writes across siblings if needed (hash-slug → repo deterministic)
- GitHub Actions matrix runner unchanged (16 shards, 7 GB each)
- **Exit semantics**: exits 0 on success, non-zero on fatal error (GitHub Actions will fail correctly)

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

import requests
from huggingface_hub import HfApi, list_repo_tree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-enrich")

# ---- config ----
HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    log.error("HF_TOKEN required")
    sys.exit(1)

HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
API = HfApi(token=HF_TOKEN)

OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ---- dedup bridge (reuse existing) ----
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

dedup = DedupStore()

# ---- helpers ----
def deterministic_shard(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def list_date_files(date_folder: str) -> List[str]:
    """Single API call to list files in date folder (non-recursive)."""
    log.info("Listing repo tree for %s", date_folder)
    items = list_repo_tree(
        repo_id=HF_REPO,
        path=date_folder,
        repo_type="dataset",
        token=HF_TOKEN,
    )
    files = [it.rfilename for it in items if it.type == "file"]
    log.info("Found %d files in %s", len(files), date_folder)
    return files

def download_via_cdn(path: str) -> bytes:
    """Download via CDN (no auth counted against API rate limits)."""
    url = f"{BASE_CDN}/{path}"
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content

def parse_parquet_to_pairs(content: bytes) -> List[Dict[str, str]]:
    """Project heterogeneous parquet to {prompt,response} only."""
    import pyarrow.parquet as pq
    import io
    table = pq.read_table(io.BytesIO(content))
    df = table.to_pandas()
    pairs = []
    for _, row in df.iterrows():
        prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
        response = row.get("response") or row.get("output") or row.get("answer") or ""
        if not prompt or not response:
            continue
        pairs.append({"prompt": str(prompt).strip(), "response": str(response).strip()})
    return pairs

def parse_jsonl_to_pairs(content: bytes) -> List[Dict[str, str]]:
    pairs = []
    for line in content.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        if not prompt or not response:
            continue
        pairs.append({"prompt": str(prompt).strip(), "response": str(response).strip()})
    return pairs

def parse_file(path: str, content: bytes) -> List[Dict[str, str]]:
    if path.endswith(".parquet"):
        return parse_parquet_to_pairs(content)
    if path.endswith(".jsonl"):
        return parse_jsonl_to_pairs(content)
    log.warning("Unsupported file %s, skipping", path)
    return []

def slug_from_path(path: str) -> str:
    return Path(path).stem

# ---- main ----
def main() -> None:
    log.info("Worker shard %d/%d for date %s", SHARD_ID, SHARD_TOTAL, DATE)

    # 1) list files once
    files = list_date_files(DATE)
    if not files:
        log.warning("No files found for %s", DATE)
        sys.exit(0)

    # save manifest for reproducibility
    manifest_path = Path(f"manifest-{DATE}.json")
    manifest_path.write_text(json.dumps(files, indent=2))
    log.info("Manifest saved to %s", manifest_path)

    # 2) process assigned shard
    processed = 0
    uploaded_pairs = 0
    out_f = OUT_FILE.open("w", encoding="utf-8")

    for fpath in sorted(files):
        slug = slug_from_path(fpath)
        if deterministic_shard(slug) != SHARD_ID:
            continue

        try:
            content = download_via_cdn(fpath)
            pairs = parse_file(fpath, content)
        except Exception as exc:
            log.exception("Failed to process %s: %s", fpath, exc)
            continue

        accepted = 0
        for pair in pairs:
            raw = f"{pair['prompt']}\n{pair['response']}".encode()
            md5 = hashlib.md5(raw).hexdigest()
            if dedup.exists(md5):
                continue
            dedup.add(md5)
            out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
            accepted += 1

        processed += 1
        uploaded_pairs += accepted
        if processed % 10 == 0:
            log.info("Processed %d files, %d pairs accepted", processed, uploaded_pairs)

    out_f.close()
    log.info("Shard complete: %d files, %d pairs -> %s", processed, uploaded_pairs, OUT_FILE)

    # 3) upload shard file to dataset repo (counts toward commit cap)
    repo_path = f"{DATE}/shard{SHARD_ID}-{TIMESTAMP}.jsonl"
    try:
        API.upload_file(
            path_or_fileobj=str(OUT_FILE),
            path_in_repo=repo_path,
            repo_id=HF_REPO,
            repo_type="dataset",
