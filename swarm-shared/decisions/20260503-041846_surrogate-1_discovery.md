# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. Add `bin/generate_manifest.py` — single API call from Mac (after rate-limit window) to list one date folder via `list_repo_tree(recursive=False)`, save `manifest.json` with CDN URLs.
2. Add `bin/worker.py` — deterministic shard worker that:
   - Reads manifest (CDN URLs only)
   - Downloads assigned files via `requests` (no HF auth, no API rate limit)
   - Projects to `{prompt, response}` only at parse time (avoids pyarrow CastError)
   - Dedups via central md5 store (`lib/dedup.py`)
   - Outputs `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
3. Update `bin/dataset-enrich.sh` → thin wrapper that calls `python bin/worker.py` with `SHARD_ID`/`TOTAL_SHARDS`.
4. Update `.github/workflows/ingest.yml` to use the Python worker and pass matrix shard params.

---

### Code Snippets

#### `bin/generate_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest for one date folder (e.g. 2026-05-03) to avoid HF API calls during training.
Usage:
  HF_TOKEN=... python bin/generate_manifest.py --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 --out manifest.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under public-raw/")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    folder = f"public-raw/{args.date}"
    entries = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)

    files = []
    for e in entries:
        if not e.path.endswith((".jsonl", ".parquet", ".json")):
            continue
        files.append({
            "path": e.path,
            "cdn_url": CDN_TEMPLATE.format(repo=args.repo, path=e.path),
            "size": getattr(e, "size", None)
        })

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": files
    }

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
Deterministic shard worker: downloads assigned files via CDN (no HF API auth),
projects to {prompt, response}, dedups, and outputs shard JSONL.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python bin/worker.py manifest.json
"""
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Local dedup store (same interface used by HF Space)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

CDN_TIMEOUT = (60, 300)  # connect, read

def hash_slug(obj: Dict[str, Any]) -> str:
    # Deterministic shard assignment
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(raw.encode()).hexdigest()

def belongs_to_shard(md5_hex: str, shard_id: int, total_shards: int) -> bool:
    bucket = int(md5_hex, 16) % total_shards
    return bucket == shard_id

def project_to_pair(obj: Dict[str, Any]) -> Dict[str, str] | None:
    """Return {prompt, response} or None if invalid."""
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
    response = obj.get("response") or obj.get("output") or obj.get("answer")
    if not prompt or not response:
        return None
    # Keep strings only
    prompt = str(prompt).strip()
    response = str(response).strip()
    if not prompt or not response:
        return None
    return {"prompt": prompt, "response": response}

def stream_jsonl(url: str) -> List[Dict[str, Any]]:
    rows = []
    resp = requests.get(url, timeout=CDN_TIMEOUT, stream=True)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows

def stream_parquet(url: str) -> List[Dict[str, Any]]:
    # Download to temp file-like buffer (avoid filesystem)
    resp = requests.get(url, timeout=CDN_TIMEOUT)
    resp.raise_for_status()
    buf = pa.BufferReader(resp.content)
    table = pq.read_table(buf)
    # Convert to list of dicts (safe for heterogeneous schemas)
    return table.to_pylist()

def process_file(
    url: str,
    dedup: DedupStore,
    shard_id: int,
    total_shards: int,
) -> List[Dict[str, str]]:
    ext = Path(url).suffix.lower()
    if ext == ".parquet":
        raw_rows = stream_parquet(url)
    else:
        raw_rows = stream_jsonl(url)

    out: List[Dict[str, str]] = []
    for row in raw_rows:
        pair = project_to_pair(row)
        if not pair:
            continue
        md5 = hash_slug(pair)
        if not belongs_to_shard(md5, shard_id, total_shards):
            continue
        if dedup.seen(md5):
            continue
        dedup.add(md5)
        out.append(pair)
    return out

def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    total_shards = int(os.getenv("TOTAL_SHARDS", "16"))
    if len(sys.argv) < 2:
        print("Usage: SHARD_ID=x TOTAL_SHARDS=n python worker.py manifest.json")
        sys.exit(1)

    manifest_path = sys.argv[1]
    with open(manifest_path) as f:
        manifest = json.load(f)

    date = manifest["date"]
    files = manifest["files"]

    # Assign deterministic subset by shard (same logic as hash-based routing)
    assigned_files = [
        f for f in files
        if belongs_to_shard(hash_slug({"path": f["path"]}), shard_id, total_shards)
    ]

    dedup = DedupStore()
    out_dir = Path(f"batches/public-merged/{date}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{shard_id}-{ts}.jsonl"

    total_pairs = 0
    with out_path.open("w") as out_f:
        for meta in tqdm(assigned_files, desc=f"Shard {shard_id}"):
            try:
                pairs = process_file(
                    meta["cdn_url"],
                    dedup=dedup,
                    shard_id=shard_id,
                    total_shards=total_shards,
                )
                for p in pairs:
                    out_f.write(json.dumps(p,
