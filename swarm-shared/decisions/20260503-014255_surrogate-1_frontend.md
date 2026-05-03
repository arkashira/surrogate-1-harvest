# surrogate-1 / frontend

## Final Implementation Plan  
**Goal:** Replace `bin/dataset-enrich.sh` with a manifest-driven, CDN-bypass ingestion worker that eliminates HF API rate limits, avoids mixed-schema `pyarrow` errors, and emits clean `{prompt, response}` JSONL shards.

---

### 1) Highest-value improvement (<2h)
- **Bypass HF API rate limits** by fetching files directly via public CDN URLs (`resolve/main/...`) with no Authorization header.
- **Avoid mixed-schema errors** by never using `load_dataset(streaming=True)` on heterogeneous repos; instead download individual files via CDN and project to `{prompt, response}` at parse time.
- **Keep existing 16-shard GitHub Actions matrix** unchanged; only the worker script and manifest handling change.

---

### 2) Concrete steps (all in `/opt/axentx/surrogate-1`)

1. **Create manifest generator** `bin/build-file-manifest.py` (run once per date folder from Mac).  
   - Lists files in `date/` prefix via one HF API call.  
   - Emits JSON with `repo`, `path`, `cdn_url`, `date` for each file.  
   - Output checked into repo or passed via workflow.

2. **Create worker** `bin/ingest-cdn-worker.py` (Python, typed, robust).  
   - Accepts `SHARD_ID`, `TOTAL_SHARDS`, and `MANIFEST` (JSON or file path).  
   - Deterministic shard assignment by content hash.  
   - Downloads via CDN (no auth).  
   - Supports `.parquet`, `.jsonl/.ndjson`, `.json`, and `.csv` with schema projection.  
   - Deduplicates via `lib/dedup.py`.  
   - Emits clean `{prompt, response}` JSONL to stdout.

3. **Update `bin/dataset-enrich.sh`** to delegate to the Python worker (preserve interface for workflow).

4. **Update `.github/workflows/ingest.yml`** to:  
   - Run `build-file-manifest.py` once per workflow run (or use pre-built manifest).  
   - Pass manifest (or embed file list) to each shard.  
   - Keep 16-shard matrix; each shard runs `ingest-cdn-worker.py` and writes to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

5. **Add minimal unit tests** for schema projection, CDN URL construction, and shard assignment.

---

### 3) `bin/build-file-manifest.py`

```python
#!/usr/bin/env python3
"""
Single API call to list one date folder in the dataset repo.
Save file list as JSON to be embedded in the GitHub Actions matrix
or passed to workers.

Usage:
  python bin/build-file-manifest.py --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 --out manifest-2026-05-03.json
"""
import argparse
import json
import sys
from typing import List, Dict

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_folder(repo: str, date: str) -> List[str]:
    api = HfApi()
    prefix = f"{date}/"
    items = api.list_repo_tree(repo=repo, path=prefix, recursive=False)
    return [it.rfilename for it in items if not it.rfilename.endswith("/")]

def build_manifest(repo: str, date: str) -> List[Dict[str, str]]:
    files = list_date_folder(repo, date)
    entries = []
    for f in files:
        entries.append({
            "repo": repo,
            "path": f,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=f),
            "date": date,
        })
    return entries

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder in dataset")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    manifest = build_manifest(args.repo, args.date)
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(manifest)} entries to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/build-file-manifest.py
```

---

### 4) `bin/ingest-cdn-worker.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven CDN-bypass ingestion worker.
Each GitHub Actions shard runs this with:
  SHARD_ID=0..15
  TOTAL_SHARDS=16
  MANIFEST_JSON='[...]'  or  MANIFEST_FILE=path.json

Behavior:
- Deterministic shard assignment by content hash.
- Download each assigned file via CDN (no Authorization header).
- Parse per known schema; project to {prompt, response}.
- Dedup via lib/dedup.py.
- Emit JSONL to stdout (workflow redirects to shard file).
"""
import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, Iterable, Optional

import requests
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.dedup import DedupStore  # type: ignore

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

# ---- schema projection ----
def try_project_record(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if isinstance(obj, dict):
        if "prompt" in obj and "response" in obj:
            return {"prompt": str(obj["prompt"]), "response": str(obj["response"])}
        if "instruction" in obj and "output" in obj:
            return {"prompt": str(obj["instruction"]), "response": str(obj["output"])}
        if "input" in obj and "output" in obj:
            return {"prompt": str(obj["input"]), "response": str(obj["output"])}
    return None

def try_project_json_lines(text: str) -> Optional[Dict[str, str]]:
    try:
        obj = json.loads(text)
        return try_project_record(obj)
    except Exception:
        return None

# ---- shard assignment ----
def compute_content_hash(obj: Dict[str, str]) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def should_process(shard_id: int, total_shards: int, content_hash: str) -> bool:
    bucket = int(content_hash, 16) % total_shards
    return bucket == shard_id

# ---- download ----
def download_cdn(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout, headers={})
    resp.raise_for_status()
    return resp.content

# ---- file processors ----
def process_parquet(content: bytes, shard_id: int, total_shards: int, dedup: DedupStore) -> Iterable[Dict[str, str]]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return
    table = pq.read_table(pa.BufferReader(content))
    for batch in table.to_batches(max_chunksize=1000):
        for row in batch.to_pylist():
            projected = try_project_record(row)
            if not projected:
                continue
            h = compute_content_hash(projected)
            if not should_process(shard_id, total_shards, h):
                continue
            if dedup.seen(h):
                continue
            dedup.add(h)
            yield projected

def process_jsonl(content: bytes, shard_id: int, total_shards: int, dedup: DedupStore) -> Iterable[Dict[str, str]]:
    for line in content.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        projected = try_project_json_lines(line)
        if not projected:
            continue
        h = compute_content_hash(projected)
       
