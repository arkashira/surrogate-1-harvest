# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix
- Uses a pre-generated `file-list.json` (created once on Mac) to avoid recursive HF API calls and 429s
- Downloads only assigned shard files via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero Authorization header, bypasses `/api/` rate limits
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids PyArrow CastError)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns
- Exits non-zero on unrecoverable errors so Actions marks shard failed

### Steps (1h 45m)

1. Create `bin/dataset-enrich.py` (60 min) — worker logic + CDN fetch + schema projection + dedup + output
2. Update `.github/workflows/ingest.yml` to pass matrix vars and generate file-list once (15 min)
3. Add small helper `bin/list-date-folder.py` for Mac to produce `file-list.json` (15 min)

---

## 1) bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 public-dataset ingestion.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date-folder 2026-05-03 \
    --file-list file-list.json \
    --out-dir batches/public-merged

Behavior:
- Reads file-list.json (generated once on Mac)
- Each entry: {"path": "2026-05-03/abc.parquet", "slug": "abc"}
- Deterministic shard: hash(slug) % SHARD_TOTAL == SHARD_ID
- Downloads via HF CDN (no Authorization header)
- Projects to {prompt, response} only at parse time
- Dedups via lib/dedup.py md5 store
- Writes shard-N-HHMMSS.jsonl
- Exits non-zero on unrecoverable errors so Actions marks shard failed
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_DATASETS_CDN = "https://huggingface.co/datasets"
RETRY_WAIT = 5
MAX_RETRIES = 5


def shard_for(slug: str, total: int) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % total


def cdn_url(repo: str, path: str) -> str:
    return f"{HF_DATASETS_CDN}/{repo}/resolve/main/{path}"


def robust_get(url: str) -> Optional[bytes]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.content
            # 404 -> skip; 429/5xx -> retry
            if resp.status_code == 404:
                return None
            wait = RETRY_WAIT * attempt
            print(f"  WARN {resp.status_code} for {url}, retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
        except requests.RequestException as exc:
            wait = RETRY_WAIT * attempt
            print(f"  WARN {exc} for {url}, retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    print(f"  ERROR failed after {MAX_RETRIES} retries: {url}", file=sys.stderr)
    return None


def project_to_pair(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Best-effort projection to {prompt, response}.
    Avoids pyarrow schema issues by operating on dict rows.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}


def extract_pairs_from_parquet(data: bytes) -> Iterable[Dict[str, str]]:
    try:
        table = pq.read_table(pq.ParquetFile(pq.ParquetBuffer(data)))
        # Convert to list of dicts to avoid schema mismatches across files
        rows = table.to_pylist()
        for row in rows:
            pair = project_to_pair(row)
            if pair:
                yield pair
    except Exception as exc:
        print(f"  WARN failed to parse parquet: {exc}", file=sys.stderr)


def slug_from_path(path: str) -> str:
    # heuristic: last component without extension
    name = Path(path).stem
    return name if name else path.replace("/", "_")


def main() -> None:
    parser = argparse.ArgumentParser(description="Surrogate-1 CDN-bypass shard worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date-folder", required=True, help="e.g. 2026-05-03")
    parser.add_argument("--file-list", required=True, help="JSON list of {path, slug?}")
    parser.add_argument("--out-dir", default="batches/public-merged")
    args = parser.parse_args()

    shard_id = int(os.environ.get("SHARD_ID", "0"))
    shard_total = int(os.environ.get("SHARD_TOTAL", "16"))
    run_ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_file = Path(args.out_dir) / args.date_folder / f"shard{shard_id}-{run_ts}.jsonl"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    with open(args.file_list) as f:
        entries = json.load(f)

    dedup = DedupStore()
    written = 0
    skipped_dup = 0
    skipped_shard = 0
    failed_download = 0

    for entry in tqdm(entries, desc=f"shard{shard_id}"):
        path = entry["path"]
        slug = entry.get("slug") or slug_from_path(path)

        if shard_for(slug, shard_total) != shard_id:
            skipped_shard += 1
            continue

        url = cdn_url(args.repo, path)
        data = robust_get(url)
        if data is None:
            failed_download += 1
            continue

        for pair in extract_pairs_from_parquet(data):
            md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
            if dedup.exists(md5):
                skipped_dup += 1
                continue
            dedup.add(md5)
            with out_file.open("a") as fh:
                fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
            written += 1

    print(
        json.dumps(
            {
                "shard_id": shard_id,
                "shard_total": shard_total,
                "date_folder": args.date_folder,
                "written": written,
                "skipped_dup": skipped_dup,
                "skipped_shard": skipped_shard,
                "failed_download": failed_download,
                "out": str(out_file),
            },
            indent=2,
        )
    )

    # Exit non-zero on unrecoverable errors so Actions marks shard failed
    if failed_download > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

---

## 2) .github/workflows/ingest.yml

```yaml
name: surrogate-1-ingest

on:
  schedule:
