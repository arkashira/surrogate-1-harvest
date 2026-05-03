# surrogate-1 / frontend

### Final Implementation Plan  
**Goal:** Replace `bin/dataset-enrich.sh` with a manifest-driven, CDN-bypass ingestion worker (`bin/dataset-enrich.py`) to improve throughput, avoid Hugging Face API rate limits, and enable reliable parallel ingestion within a 2-hour delivery window.

---

### 1. Core Design Decisions (Resolved Contradictions)

- **Manifest-driven ingestion (from Candidate 2) is required.**  
  A date-stamped manifest file (e.g., `manifest/2024-06-01.json`) lists exact CDN URLs to fetch. This enables deterministic sharding, retries, and parallelism.

- **CDN-bypass means direct `resolve_main` + raw CDN URLs (Candidate 2), not dataset-level API calls (Candidate 1).**  
  Use `huggingface_hub` to resolve files to CDN URLs, then stream via `requests`. Do not use `/upload` POST endpoints from the worker (Candidate 1) — workers should only download; CI/CD or a separate uploader handles repo writes.

- **No inline upload in worker (reject Candidate 1 upload logic).**  
  Workers fetch and write locally (or to a temp bucket). A separate step or CI job commits to `axentx/surrogate-1-training-pairs`. This avoids token exposure in workers and keeps concerns separated.

- **CLI args + env vars (Candidate 2) over env-only (Candidate 1).**  
  Required for explicit sharding and local testing.

- **Concurrency via `ThreadPoolExecutor` (Candidate 2) is correct.**  
  I/O-bound workload; parallel downloads maximize CDN bandwidth without complex async refactors.

---

### 2. Final Script: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven CDN-bypass ingestion worker.
Downloads dataset shards directly from HuggingFace CDN.
"""

import os
import sys
import json
import argparse
import requests
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import huggingface_hub

DEFAULT_REPO = "axentx/surrogate-1-training-pairs"
DEFAULT_MAX_WORKERS = 8
DEFAULT_OUTPUT_ROOT = Path("./ingest_cache")

def parse_args():
    parser = argparse.ArgumentParser(description="CDN-bypass dataset enrichment worker")
    parser.add_argument("--shard_id", type=int, required=True, help="Current shard index")
    parser.add_argument("--shard_total", type=int, default=16, help="Total number of shards")
    parser.add_argument("--date", type=str, required=True, help="Ingestion date (YYYY-MM-DD)")
    parser.add_argument("--hf_token", type=str, default=os.getenv("HF_TOKEN"), help="HuggingFace token")
    parser.add_argument("--repo_id", type=str, default=DEFAULT_REPO, help="Repo id (for manifest/logging)")
    parser.add_argument("--manifest_dir", type=str, default="manifest", help="Directory containing manifests")
    parser.add_argument("--output_root", type=str, default=str(DEFAULT_OUTPUT_ROOT), help="Output directory")
    parser.add_argument("--max_workers", type=int, default=DEFAULT_MAX_WORKERS, help="Parallel download threads")
    return parser.parse_args()

def load_manifest(manifest_path: Path):
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Expected format: {"files": ["path/in/repo/file1.parquet", ...]}
    files = data.get("files") or data if isinstance(data, list) else data.get("files", [])
    if not files:
        raise ValueError("Manifest contains no files")
    return files

def resolve_to_cdn_url(hf_api, repo_id, repo_path):
    """Resolve repo file to a CDN URL."""
    try:
        # huggingface_hub resolves to a temporary CDN-signed URL
        url = hf_api.resolve_main(repo_id, repo_path)
        return str(url)
    except Exception as e:
        raise RuntimeError(f"Failed to resolve {repo_id}/{repo_path}: {e}")

def download_file(url, dest_path, hf_token=None):
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=30) as r:
            r.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return {"url": url, "dest": str(dest_path), "status": "ok"}
    except Exception as e:
        return {"url": url, "dest": str(dest_path), "status": "error", "error": str(e)}

def shard_filter(files, shard_id, shard_total):
    """Deterministically assign files to shards by hash."""
    import hashlib
    assigned = []
    for f in files:
        h = int(hashlib.sha256(f.encode("utf-8")).hexdigest(), 16)
        if h % shard_total == shard_id:
            assigned.append(f)
    return assigned

def main():
    args = parse_args()
    if not args.hf_token:
        print("ERROR: HF_TOKEN is required (env or --hf_token)", file=sys.stderr)
        sys.exit(1)

    manifest_path = Path(args.manifest_dir) / f"{args.date}.json"
    output_root = Path(args.output_root) / args.date
    output_root.mkdir(parents=True, exist_ok=True)

    hf_api = huggingface_hub.HfApi(token=args.hf_token)

    try:
        files = load_manifest(manifest_path)
    except Exception as e:
        print(f"ERROR loading manifest: {e}", file=sys.stderr)
        sys.exit(1)

    shard_files = shard_filter(files, args.shard_id, args.shard_total)
    print(f"Shard {args.shard_id}/{args.shard_total}: processing {len(shard_files)} files")

    # Resolve all to CDN URLs first (fail fast on resolution errors)
    tasks = []
    for repo_path in shard_files:
        try:
            url = resolve_to_cdn_url(hf_api, args.repo_id, repo_path)
            dest = output_root / repo_path
            tasks.append((url, dest))
        except Exception as e:
            print(f"WARNING: skipping {repo_path}: {e}")

    # Parallel download
    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {
            executor.submit(download_file, url, dest, args.hf_token): (url, dest)
            for url, dest in tasks
        }
        for future in as_completed(futures):
            res = future.result()
            results.append(res)
            if res["status"] == "ok":
                print(f"OK: {res['dest']}")
            else:
                print(f"FAIL: {res['dest']} -> {res.get('error')}")

    failed = [r for r in results if r["status"] == "error"]
    if failed:
        print(f"Completed with {len(failed)} failures out of {len(results)} files", file=sys.stderr)
        # Non-zero exit can be tuned based on policy; fail CI on any failure:
        sys.exit(1)

    print("All files downloaded successfully.")

if __name__ == "__main__":
    main()
```

---

### 3. Workflow Changes

**File: `.github/workflows/ingest.yml` (or equivalent)**

```yaml
name: Ingest

on:
  workflow_dispatch:
    inputs:
      date:
        description: "Ingestion date (YYYY-MM-DD)"
        required: true
  schedule:
    - cron: "0 2 * * *"  # daily at 02:00 UTC

jobs:
  ingest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
        shard_total: [16]
    steps:
      - uses: actions/checkout@v4

      - name
