# surrogate-1 / frontend

**Final Unified Implementation (highest value, <2 h, production-ready)**

Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that:
- eliminates HF API rate limits during data loads,
- prevents mixed-schema `pyarrow.CastError`s,
- preserves existing 16-shard parallelism and dedup semantics,
- is concrete and immediately actionable.

---

## 1) What we ship (single source of truth)

- `bin/gen_manifest.py`  
  One-shot script (run once per cron) that lists a date folder via HF API (single call) and writes `manifest.json` with CDN URLs.

- `bin/ingest_worker.py`  
  Shard worker that:
  - reads `manifest.json`,
  - deterministically selects its 1/N shard,
  - downloads files via HF CDN direct URLs (no Authorization header → bypasses `/api/` rate limits),
  - projects heterogeneous files to `{prompt, response}` at parse time,
  - deduplicates via existing `lib/dedup.py` md5 store,
  - writes `batches/public-merged/<date>/shard-<SHARD_ID>-<HHMMSS>.jsonl`.

- `bin/dataset-enrich.sh`  
  Thin wrapper that sets env and calls `python bin/ingest_worker.py`.

- GitHub Actions (`ingest.yml`) unchanged (16-shard matrix) — each runner gets its own RAM and runs the same worker.

---

## 2) Code (copy-paste ready)

### `bin/gen_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest for a single date folder.
Run once per cron. Outputs manifest.json with CDN paths.

Usage:
  python bin/gen_manifest.py --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 --out manifest.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder in repo")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    api = HfApi()
    try:
        entries = api.list_repo_tree(
            repo_id=args.repo,
            path=args.date,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        print(f"Failed to list {args.repo}/{args.date}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = []
    for e in entries:
        # Accept both objects with .path and dict-like items
        path = getattr(e, "path", None) or (e if isinstance(e, str) else e.get("path"))
        if not path:
            continue
        # Skip subfolders
        if getattr(e, "type", None) == "dir" or (isinstance(e, dict) and e.get("type") == "dir"):
            continue
        size = getattr(e, "size", 0) or (isinstance(e, dict) and e.get("size", 0)) or 0
        files.append({
            "path": path,
            "cdn_url": f"https://huggingface.co/datasets/{args.repo}/resolve/main/{path}",
            "size": int(size) if size else 0,
        })

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

### `bin/ingest_worker.py`
```python
#!/usr/bin/env python3
"""
Shard worker: downloads assigned files via CDN, normalizes to {prompt, response},
dedups, and writes JSONL.

Usage:
  SHARD_ID=0 SHARD_COUNT=16 DATE=2026-05-03 MANIFEST=manifest.json \
    python bin/ingest_worker.py --out batches/public-merged/2026-05-03/shard-0-120000.jsonl
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.dedup import DedupStore  # type: ignore

USER_AGENT = "axentx-surrogate-1-worker/1.0"

def shard_select(items: List[Dict[str, Any]], shard_id: int, shard_count: int) -> List[Dict[str, Any]]:
    selected = []
    for item in items:
        path = item.get("path") or ""
        h = int(hashlib.md5(path.encode()).hexdigest(), 16)
        if h % shard_count == shard_id:
            selected.append(item)
    return selected

def download_cdn(url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()

def project_to_pair(obj: Any) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous schema to {prompt, response}.
    Returns None if unusable.
    """
    if not isinstance(obj, dict):
        return None

    low = {str(k).lower(): v for k, v in obj.items()}
    prompt_keys = {"prompt", "instruction", "input", "question", "query"}
    response_keys = {"response", "completion", "output", "answer"}

    prompt = None
    for k in prompt_keys:
        v = low.get(k)
        if isinstance(v, str) and v.strip():
            prompt = v.strip()
            break
    if prompt is None:
        # fallback: first text-like field
        for v in low.values():
            if isinstance(v, str) and v.strip():
                prompt = v.strip()
                break

    response = None
    for k in response_keys:
        v = low.get(k)
        if isinstance(v, str) and v.strip():
            response = v.strip()
            break
    if response is None:
        # fallback: any remaining text-like field different from prompt
        for k, v in low.items():
            if isinstance(v, str) and v.strip() and v.strip() != prompt:
                response = v.strip()
                break

    if not prompt or not response:
        return None
    return {"prompt": prompt, "response": response}

def read_parquet_bytes(data: bytes) -> pa.Table:
    return pq.read_table(pa.BufferReader(data))

def read_jsonl_lines(lines: List[bytes]) -> List[Dict[str, Any]]:
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
            out.append(obj)
        except Exception:
            continue
    return out

def process_file(item: Dict[str, Any], dedup: DedupStore) -> List[Dict[str, str]]:
    url = item.get("cdn_url") or ""
    if not url:
        return []
    raw = download_cdn(url)
    path = item.get("path", "").lower()

    rows: List[Dict[str, Any]] = []
    try:
        if path.endswith(".parquet") or path.endswith(".pq"):
            table = read_parquet_bytes(raw)
            rows = table.to_pylist()
        else:
            # Assume JSONL
            lines = raw.splitlines()
            rows = read_jsonl_lines(lines)
    except Exception:
        # Best
