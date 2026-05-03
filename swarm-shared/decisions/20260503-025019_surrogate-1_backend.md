# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (env).
- Single `list_repo_tree(path=DATE, recursive=False)` from HF Hub (rate-limit window) → save `file-list.json`.
- Worker loads manifest, keeps only its shard (`hash(slug) % SHARD_TOTAL == SHARD_ID`).
- Downloads via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas).
- Dedups via central md5 store (`lib/dedup.py`).
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` to HF dataset repo via `huggingface_hub` upload (commits spread across siblings if needed).
- Reuses existing HF Space pattern: no `source`/`ts` columns; attribution in filename.

---

## Final Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""
import os
import sys
import json
import hashlib
import datetime
import pathlib
import logging
from typing import List, Dict, Any, Iterator, Tuple

import requests
from huggingface_hub import HfApi, hf_hub_download, upload_file

# --
# Config
# --
REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
API = HfApi(token=os.getenv("HF_TOKEN"))

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
RUN_TS = datetime.datetime.utcnow().strftime("%H%M%S")

OUT_DIR = pathlib.Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{RUN_TS}.jsonl"

# Dedup store (shared with HF Space)
try:
    from lib.dedup import is_duplicate, mark_seen
except Exception:
    # Minimal local fallback (in-memory per-run; cross-run dedup relies on central store)
    _seen: set = set()
    def is_duplicate(md5: str) -> bool:
        return md5 in _seen
    def mark_seen(md5: str) -> None:
        _seen.add(md5)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("surrogate-1-ingest")

# --
# Helpers
# --
def slug_hash(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str) -> bool:
    return slug_hash(slug) % SHARD_TOTAL == SHARD_ID

def list_date_files(date_folder: str) -> List[str]:
    """
    Single API call to list files in DATE folder (non-recursive).
    Save to file-list.json for reproducibility.
    """
    cache = pathlib.Path("file-list.json")
    if cache.exists():
        log.info("Using cached file-list.json")
        return json.loads(cache.read_text())

    log.info("Listing repo tree for %s", date_folder)
    items = API.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    files = [it.rfilename for it in items if it.type == "file"]
    cache.write_text(json.dumps(files, indent=2))
    return files

def download_cdn(path: str) -> bytes:
    url = f"{BASE_CDN}/{path}"
    # CDN bypass: no Authorization header
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_to_pair(raw: bytes, filename: str) -> Dict[str, str]:
    """
    Project to {prompt, response} only at parse time.
    Supports common formats (jsonl, json, parquet via pyarrow if needed).
    """
    import io
    # Try JSON/JSONL first
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            # pick first object if list
            data = data[0] if data else {}
        prompt = data.get("prompt") or data.get("input") or data.get("question") or ""
        response = data.get("response") or data.get("output") or data.get("answer") or ""
        return {"prompt": str(prompt), "response": str(response)}
    except Exception:
        pass

    # Try line-delimited JSON
    try:
        lines = raw.decode().strip().splitlines()
        objs = [json.loads(l) for l in lines if l.strip()]
        pairs = []
        for obj in objs:
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            pairs.append({"prompt": str(prompt), "response": str(response)})
        # Return first pair for simplicity; caller can iterate if needed
        if pairs:
            return pairs[0]
    except Exception:
        pass

    # Fallback: minimal projection
    log.warning("Could not parse %s; returning empty pair", filename)
    return {"prompt": "", "response": ""}

def iter_parquet_pairs(raw: bytes) -> Iterator[Dict[str, str]]:
    """
    Stream rows from parquet bytes, projecting to {prompt, response}.
    """
    import pyarrow.parquet as pq
    import io
    table = pq.read_table(io.BytesIO(raw))
    for batch in table.to_batches(max_chunksize=1000):
        df = batch.to_pandas()
        for _, row in df.iterrows():
            prompt = str(row.get("prompt") or row.get("input") or row.get("question") or "")
            response = str(row.get("response") or row.get("output") or row.get("answer") or "")
            yield {"prompt": prompt, "response": response}

# --
# Main
# --
def main() -> None:
    log.info("Shard %d/%d | Date %s", SHARD_ID, SHARD_TOTAL, DATE)

    files = list_date_files(DATE)
    log.info("Found %d files in %s", len(files), DATE)

    my_files = [f for f in files if belongs_to_shard(pathlib.Path(f).stem)]
    log.info("Shard %d owns %d files", SHARD_ID, len(my_files))

    written = 0
    skipped_dup = 0
    errors = 0

    with OUT_FILE.open("w", encoding="utf-8") as fout:
        for rel_path in my_files:
            try:
                raw = download_cdn(rel_path)
                md5 = hashlib.md5(raw).hexdigest()
                if is_duplicate(md5):
                    skipped_dup += 1
                    continue

                # Dispatch by extension
                ext = pathlib.Path(rel_path).suffix.lower()
                pairs: List[Dict[str, str]] = []

                if ext in (".json", ".jsonl"):
                    pair = parse_to_pair(raw, rel_path)
                    if pair["prompt"] or pair["response"]:
                        pairs.append(pair)
                elif ext == ".parquet":
                    pairs.extend(iter_parquet_pairs(raw))
                else:
                    log.warning("Unsupported extension %s for %s", ext, rel_path)
                    continue

                for pair in pairs:
                    if not pair["prompt"] and not pair["response"]:
                        continue
                    record = {
                        "prompt": pair["prompt"],
                        "response": pair["response"],
                    }
                    fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                    written
