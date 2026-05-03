# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs and integrity metadata, plus a training script that uses **CDN-only** fetches with streaming, integrity checks, and zero HF API calls during training. This implements the CDN bypass pattern and prevents 429s while training on Lightning.

### Steps (1h 45m total)

1. **Create `tools/snapshot_manifest.py`** (30m)  
   - Single `list_repo_tree` call for `public-merged/<date>/` (non-recursive)  
   - Emit `file_manifest.json` with `{date, prefix, files: [{path, cdn_url, size, md5}]}`  
   - Include retry/backoff for 429s (max 3 retries with exponential backoff)  
   - Validate HF token presence before calling API

2. **Create `train_cdn.py`** (45m)  
   - Load `file_manifest.json`  
   - Stream parquet from CDN URLs with `requests` + `pyarrow.parquet`  
   - Verify integrity via `md5` when available (skip on mismatch)  
   - Project `{prompt, response}` only; handle schema heterogeneity  
   - Zero HF API calls during training; log any CDN failures

3. **Update Lightning launcher** (10m)  
   - Accept `--manifest file_manifest.json`  
   - Reuse running Studio if present; fallback to L40S on `lightning-public-prod`

4. **Add cron/workflow note** (10m)  
   - Document: run snapshot once per day after ingest completes  
   - Commit manifest to repo or upload as artifact

5. **Test locally** (20m)  
   - Dry-run manifest on a small date partition  
   - Verify CDN fetch + parse + integrity checks  
   - Confirm zero HF API calls via logging

---

## tools/snapshot_manifest.py

```python
#!/usr/bin/env python3
"""
snapshot_manifest.py
List one date-partition of axentx/surrogate-1-training-pairs via a single
HF API call and emit file_manifest.json with CDN URLs and integrity metadata.

Usage:
    python tools/snapshot_manifest.py --date 2026-05-03 --out file_manifest.json
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, List

from huggingface_hub import HfApi, hf_hub_url

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

def _retry_on_429(fn, max_retries=3, backoff_factor=2.0):
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if "429" in str(e) and attempt < max_retries - 1:
                wait = backoff_factor ** attempt
                print(f"Rate limited (429). Retry {attempt + 1}/{max_retries} in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise

def build_manifest(date: str, out_path: str) -> Dict:
    api = HfApi()
    prefix = f"public-merged/{date}/"

    # Single API call: non-recursive tree listing for the date folder
    def _list_tree():
        return api.list_repo_tree(
            repo_id=REPO_ID,
            path=prefix,
            repo_type="dataset",
            recursive=False,
        )

    try:
        tree = _retry_on_429(_list_tree)
    except Exception as e:
        print(f"HF API error listing {prefix}: {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        if not entry.path.lower().endswith((".parquet", ".jsonl")):
            continue

        cdn_url = f"{BASE_CDN}/{entry.path}"
        files.append({
            "path": entry.path,
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
            "md5": getattr(entry, "lfs", {}).get("oid", None) if hasattr(entry, "lfs") else None,
        })

    manifest = {
        "date": date,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "repo_id": REPO_ID,
        "prefix": prefix,
        "count": len(files),
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote manifest for {len(files)} files -> {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN manifest for a date partition")
    parser.add_argument("--date", required=True, help="Date partition (YYYY-MM-DD)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.date, args.out)
```

---

## train_cdn.py

```python
#!/usr/bin/env python3
"""
train_cdn.py
Training data loader that uses CDN-only fetches (zero HF API calls) with integrity checks.

Usage:
    python train_cdn.py --manifest file_manifest.json [--limit N]
"""

import argparse
import hashlib
import json
import sys
from io import BytesIO
from typing import Iterator, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def stream_parquet_from_cdn(url: str, expected_md5: str = None) -> pa.Table:
    """Download parquet via CDN and return pyarrow Table; verify md5 if provided."""
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    raw = resp.content
    if expected_md5 and _md5(raw) != expected_md5:
        raise ValueError(f"MD5 mismatch for {url}")
    return pq.read_table(BytesIO(raw))


def iter_examples(manifest_path: str, limit: int = None) -> Iterator[Dict[str, Any]]:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    count = 0
    for file_info in manifest["files"]:
        url = file_info["cdn_url"]
        md5 = file_info.get("md5")
        try:
            table = stream_parquet_from_cdn(url, expected_md5=md5)
        except Exception as e:
            print(f"Failed to fetch {url}: {e}", file=sys.stderr)
            continue

        # Project only required columns; tolerate schema heterogeneity
        cols = set(table.column_names)
        if "prompt" not in cols or "response" not in cols:
            print(f"Skipping {url}: missing prompt/response", file=sys.stderr)
            continue

        prompts = table["prompt"].to_pylist()
        responses = table["response"].to_pylist()

        for p, r in zip(prompts, responses):
            if p is None or r is None:
                continue
            yield {"prompt": str(p), "response": str(r)}
            count += 1
            if limit is not None and count >= limit:
                return


def main():
    parser = argparse.ArgumentParser(description="CDN-based training data loader")
    parser.add_argument("--manifest", required=True, help="Path to file_manifest.json")
    parser.add_argument("--limit", type=int, default=None, help="Limit examples")
    args = parser.parse_args()

    for ex in iter_examples(args.manifest, limit=args.limit):
        # Replace with your training collator / tokenization
        print(json.dumps(ex, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

