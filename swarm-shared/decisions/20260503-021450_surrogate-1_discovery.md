# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side, single `list_repo_tree` call) to deterministically shard file paths without recursive API pagination.
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to avoid 429 rate limits.
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids `pyarrow.CastError` from `load_dataset(streaming=True)`).
- Deduplicates via central `lib/dedup.py` md5 store and writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Adds retry/backoff for CDN 429 and commit-cap spreading across sibling repos if needed.

### Steps (1h 45m)

1. (10m) Inspect current `bin/dataset-enrich.sh` and `lib/dedup.py` to confirm interfaces.
2. (20m) Write `bin/dataset-enrich.py`:
   - Shebang `#!/usr/bin/env bash` wrapper that invokes `python3 bin/dataset-enrich.py "$@"` (keeps cron compatibility).
   - Python worker: parse `SHARD_ID`, `SHARD_TOTAL`; load `file-list.json`; shard by `hash(slug) % SHARD_TOTAL`.
   - CDN downloader with `requests.get(cdn_url, timeout=30, stream=True)`; retry on 429 (wait 360s).
   - Schema projector: detect common HF dataset formats (jsonl, parquet via `pyarrow`), extract `prompt`/`response` fields with fallbacks.
   - Dedup via `lib/dedup.py` (md5 of normalized pair).
   - Output to `batches/public-merged/<date>/shard{N}-<ts>.jsonl`.
3. (15m) Add `bin/generate-file-list.py` (Mac-side): single `list_repo_tree(recursive=False)` per date folder → `file-list.json`. Embed path in workflow or generate once per day.
4. (20m) Update `.github/workflows/ingest.yml`:
   - Add step to fetch latest `file-list.json` (or generate via Mac runner once/day and commit).
   - Pass `SHARD_ID`, `SHARD_TOTAL` to matrix.
   - Set `SHELL=/bin/bash` in job defaults.
5. (30m) Test locally:
   - Simulate 16 shards on a small file list.
   - Verify CDN downloads, schema projection, dedup, and output format.
   - Confirm no HF API auth calls during data load.
6. (20m) Harden:
   - Respect HF commit cap: if writing to `axentx/surrogate-1-training-pairs`, optionally spread across sibling repos by deterministic hash (not required if only appending to dataset repo).
   - Idle-stop resilience: GitHub Actions runs are stateless; no Lightning Studio reuse needed here.
7. (10m) Cleanup: remove old `.sh` or keep as wrapper.

---

## Code Snippets

### bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage: SHARD_ID=0 SHARD_TOTAL=16 python3 bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --file-list file-list.json \
    --out-dir batches/public-merged
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
import pyarrow.parquet as pq
import pyarrow as pa

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
RETRY_WAIT = 360  # seconds after 429
MAX_RETRIES = 3

def shard_match(slug: str, shard_id: int, shard_total: int) -> bool:
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return (h % shard_total) == shard_id

def cdn_download(url: str, timeout: int = 30) -> bytes:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            if resp.status_code == 429:
                wait = RETRY_WAIT
                print(f"CDN 429, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")

def extract_pair(record: Dict[str, Any]) -> Optional[Dict[str, str]]:
    # Common field names
    prompt_fields = ["prompt", "instruction", "input", "question", "text"]
    response_fields = ["response", "completion", "output", "answer"]

    prompt = None
    for f in prompt_fields:
        if f in record and isinstance(record[f], str) and record[f].strip():
            prompt = record[f].strip()
            break
    response = None
    for f in response_fields:
        if f in record and isinstance(record[f], str) and record[f].strip():
            response = record[f].strip()
            break

    if prompt is None or response is None:
        return None
    return {"prompt": prompt, "response": response}

def process_parquet(content: bytes) -> List[Dict[str, str]]:
    table = pq.read_table(pa.BufferReader(content))
    rows = table.to_pylist()
    out = []
    for row in rows:
        pair = extract_pair(row)
        if pair:
            out.append(pair)
    return out

def process_jsonl(content: bytes) -> List[Dict[str, str]]:
    out = []
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        pair = extract_pair(obj)
        if pair:
            out.append(pair)
    return out

def process_file(repo: str, path: str) -> List[Dict[str, str]]:
    url = HF_CDN_TEMPLATE.format(repo=repo, path=path)
    content = cdn_download(url)
    if path.endswith(".parquet"):
        return process_parquet(content)
    elif path.endswith(".jsonl"):
        return process_jsonl(content)
    else:
        # Try parquet first, then jsonl fallback
        try:
            return process_parquet(content)
        except Exception:
            try:
                return process_jsonl(content)
            except Exception:
                print(f"Unsupported file: {path}", file=sys.stderr)
                return []

def main() -> None:
    shard_id = int(os.environ.get("SHARD_ID", "0"))
    shard_total = int(os.environ.get("SHARD_TOTAL", "16"))
    repo = os.environ.get("REPO", "axentx/surrogate-1-training-pairs")
    date = os.environ.get("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
    file_list_path = os.environ.get("FILE_LIST", "file-list.json")
    out_dir = Path(os.environ.get("OUT_DIR", "batches/public-merged"))

    with open(file_list_path) as f:
        file_list = json.load(f)  # list of relative paths under dataset repo

    my_files = [
        p for p in file_list
        if shard_match(p, shard_id, shard_total)
    ]

    dedup = DedupStore()
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / date / f"shard{shard_id}-{ts}.
