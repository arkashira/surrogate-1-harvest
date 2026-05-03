# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. Add `bin/ingest_worker.py` — deterministic shard worker that:
   - Reads a pre-generated `file-manifest.json` (single API call from Mac) listing target files for a date folder
   - Downloads each file via HF CDN (`resolve/main/...`) with no Authorization header (bypasses `/api/` rate limits)
   - Projects heterogeneous schemas to `{prompt, response}` only at parse time
   - Deduplicates via centralized `lib/dedup.py` md5 store
   - Writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`

2. Add `bin/gen_manifest.py` — run once per date folder from Mac:
   - Calls `list_repo_tree(path, recursive=False)` per folder (no recursive paginated `list_repo_files`)
   - Emits `file-manifest.json` with `{"date": "YYYY-MM-DD", "files": ["path1", ...]}`

3. Update `bin/dataset-enrich.sh` → thin wrapper that invokes `python bin/ingest_worker.py` with `SHARD_ID`, `MANIFEST_PATH`, `DATE`.

4. Update `.github/workflows/ingest.yml`:
   - Pass `DATE` and `MANIFEST_PATH` as env inputs
   - Keep 16-shard matrix strategy
   - Ensure `python -m pip install -r requirements.txt`

5. Bump `requirements.txt` with `requests` (for CDN downloads) if not present.

---

### Code Snippets

#### `bin/gen_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate file manifest for a date folder to avoid recursive HF API calls.
Run from Mac after rate-limit window clears.
Usage:
  python bin/gen_manifest.py --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 --out file-manifest.json
"""
import argparse
import json
import os
import sys

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    # List top-level objects in the date folder; avoid recursive=True on big repos.
    folder = f"batches/public-merged/{args.date}"
    try:
        items = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
    except Exception as e:
        print(f"Failed to list {folder}: {e}", file=sys.stderr)
        sys.exit(1)

    files = [it.path for it in items if it.type == "file"]
    manifest = {"date": args.date, "files": sorted(files)}
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

#### `bin/ingest_worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingest worker for a deterministic shard.
Avoids HF API rate limits during data loading by using public CDN URLs.
Projects mixed schemas to {prompt, response} only at parse time.
"""
import json
import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_DATASETS_ROOT = "https://huggingface.co/datasets"
REPO = "axentx/surrogate-1-training-pairs"

def hf_cdn_url(file_path: str) -> str:
    # Public CDN URL — no Authorization header required
    return f"{HF_DATASETS_ROOT}/{REPO}/resolve/main/{file_path}"

def hash_slug(file_path: str, record: Dict[str, Any]) -> str:
    content = f"{file_path}|{record.get('prompt','')}|{record.get('response','')}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()

def project_record(raw: Dict[str, Any]) -> Dict[str, str]:
    # Project heterogeneous schemas to canonical {prompt, response}
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def stream_parquet_cdn(url: str) -> List[Dict[str, str]]:
    # Download via CDN then decode; avoids load_dataset(streaming=True) on mixed schemas
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    table = pq.read_table(pa.BufferReader(resp.content))
    records = []
    for batch in table.to_batches():
        cols = {name: batch.column(name).to_pylist() for name in batch.schema.names}
        n = len(next(iter(cols.values()))) if cols else 0
        for i in range(n):
            raw = {k: cols[k][i] for k in cols}
            records.append(project_record(raw))
    return records

def stream_jsonl_cdn(url: str) -> List[Dict[str, str]]:
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    records = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        records.append(project_record(raw))
    return records

def main() -> None:
    shard_id = int(os.environ["SHARD_ID"])  # 0..15
    total_shards = int(os.environ.get("TOTAL_SHARDS", "16"))
    manifest_path = os.environ.get("MANIFEST_PATH", "file-manifest.json")
    date = os.environ.get("DATE", "")

    with open(manifest_path) as f:
        manifest = json.load(f)

    files = manifest.get("files", [])
    if not files:
        print("No files in manifest; exiting.")
        return

    # Deterministic shard assignment by slug hash
    my_files = [fp for fp in files if (abs(int(hashlib.md5(fp.encode()).hexdigest(), 16)) % total_shards) == shard_id]
    print(f"Shard {shard_id}/{total_shards} assigned {len(my_files)} files")

    dedup = DedupStore()
    out_dir = Path(f"batches/public-merged/{manifest['date']}")
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = Path(manifest_path).stem.split("-")[-1] or "000000"
    out_path = out_dir / f"shard{shard_id}-{timestamp}.jsonl"

    written = 0
    with out_path.open("w") as out_f:
        for file_path in tqdm(my_files, desc="Shard files"):
            url = hf_cdn_url(file_path)
            try:
                if file_path.endswith(".parquet"):
                    records = stream_parquet_cdn(url)
                elif file_path.endswith(".jsonl"):
                    records = stream_jsonl_cdn(url)
                else:
                    print(f"Skipping unsupported {file_path}")
                    continue
            except Exception as e:
                print(f"Failed to process {file_path}: {e}")
                continue

            for rec in records:
                slug = hash_slug(file_path, rec)
                if dedup.seen(slug):
                    continue
                dedup.add(slug)
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1

    print(f"Shard {shard_id} wrote {written} records to {out_path}")
    # Persist dedup store if needed (central store on HF Space remains source of truth)

if __name__ == "__main__":
    main()
```

#### `bin/dataset-enrich.sh`
```bash
#!/
