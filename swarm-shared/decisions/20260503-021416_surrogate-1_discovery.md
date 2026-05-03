# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side, one API call per date folder) to avoid recursive `list_repo_files` and HF API rate limits.
- Downloads only assigned shard files via HF CDN (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, bypasses `/api/` 429 limits.
- Projects heterogeneous schemas to `{prompt, response}` **only at parse time** (avoids pyarrow CastError from `load_dataset(streaming=True)`).
- Deduplicates via central `lib/dedup.py` md5 store.
- Writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` with deterministic filenames to prevent collisions.
- Reuses existing Lightning Studio pattern for orchestration (not in this worker) and keeps Mac-only orchestration.

---

### Steps (timed)

1. **Create `bin/dataset-enrich.py`** (60 min) — manifest loader, CDN downloader, schema projector, shard assignment, dedup writer.
2. **Update `.github/workflows/ingest.yml`** (15 min) — ensure matrix passes `SHARD_ID`/`SHARD_TOTAL`, installs deps, runs new script.
3. **Add helper `bin/gen-file-list.py`** (15 min) — Mac-side script to call `list_repo_tree` once per date folder and emit `file-list.json`.
4. **Smoke test** (30 min) — run locally with a small shard, verify output format and dedup behavior.

Total: ~2h.

---

### Code Snippets

#### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --manifest file-list.json \
    --out-dir batches/public-merged
"""

import json
import os
import sys
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Iterable

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from tqdm import tqdm

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

HF_DATASETS_CDN = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
RETRY_WAIT = 360  # seconds after 429

def shard_assign(key: str, total: int) -> int:
    """Deterministic shard assignment by md5 hash."""
    digest = hashlib.md5(key.encode()).hexdigest()
    return int(digest, 16) % total

def load_manifest(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)

def cdn_download(url: str, timeout: int = 30) -> bytes:
    for attempt in range(5):
        resp = requests.get(url, timeout=timeout, stream=True)
        if resp.status_code == 429:
            wait = RETRY_WAIT
            print(f"Rate limited 429, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.content
    raise RuntimeError(f"Failed to download {url} after retries")

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schema to {prompt, response}.
    Heuristic: look for common field names; fallback to first/second text-like fields.
    """
    # Common patterns
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    prompt = None
    response = None

    for k, v in raw.items():
        if k in prompt_keys and isinstance(v, str) and v.strip():
            prompt = v.strip()
        if k in response_keys and isinstance(v, str) and v.strip():
            response = v.strip()

    if prompt is None or response is None:
        # Fallback: pick first two string fields
        str_fields = [v for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(str_fields) >= 2:
            prompt, response = str_fields[0].strip(), str_fields[1].strip()
        else:
            # Last resort: serialize
            prompt = json.dumps(raw, ensure_ascii=False)
            response = ""

    return {"prompt": prompt, "response": response}

def process_parquet(content: bytes, dedup: DedupStore) -> Iterable[Dict[str, str]]:
    table = pq.read_table(pa.BufferReader(content))
    for batch in table.to_batches(max_chunksize=1000):
        for i in range(batch.num_rows):
            row = {k: batch[k][i].as_py() for k in batch.schema.names}
            # Build deterministic hash for dedup
            raw_json = json.dumps(row, sort_keys=True, ensure_ascii=False)
            md5 = hashlib.md5(raw_json.encode()).hexdigest()
            if dedup.exists(md5):
                continue
            pair = project_to_pair(row)
            if pair["prompt"] and pair["response"]:
                dedup.add(md5)
                yield pair

def process_jsonl(content: bytes, dedup: DedupStore) -> Iterable[Dict[str, str]]:
    for line in content.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        raw_json = json.dumps(row, sort_keys=True, ensure_ascii=False)
        md5 = hashlib.md5(raw_json.encode()).hexdigest()
        if dedup.exists(md5):
            continue
        pair = project_to_pair(row)
        if pair["prompt"] and pair["response"]:
            dedup.add(md5)
            yield pair

def main():
    shard_id = int(os.environ.get("SHARD_ID", 0))
    shard_total = int(os.environ.get("SHARD_TOTAL", 16))
    repo = os.environ.get("REPO", "axentx/surrogate-1-training-pairs")
    date = os.environ.get("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
    manifest_path = Path(os.environ.get("MANIFEST", "file-list.json"))
    out_dir = Path(os.environ.get("OUT_DIR", "batches/public-merged"))

    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest(manifest_path)
    date_files = manifest.get(date, [])
    if not date_files:
        print(f"No files for date {date} in manifest", file=sys.stderr)
        sys.exit(0)

    # Assign files to shards deterministically
    my_files = [f for f in date_files if shard_assign(f, shard_total) == shard_id]
    print(f"Shard {shard_id}/{shard_total} processing {len(my_files)} files", file=sys.stderr)

    dedup = DedupStore()
    out_dir = Path(out_dir) / date
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%H%M%S")
    outfile = out_dir / f"shard{shard_id}-{timestamp}.jsonl"

    processed = 0
    with open(outfile, "w", encoding="utf-8") as fout:
        for rel_path in tqdm(my_files, desc=f"Shard {shard_id}"):
            url = HF_DATASETS_CDN.format(repo=repo, path=rel_path)
            try:
                content = cdn_download(url)
            except Exception as e:
                print(f"Failed {rel_path}: {e}", file=sys.stderr)
                continue

            if rel_path.endswith(".parquet"):
                pairs = process_parquet(content, dedup)
            elif rel_path
