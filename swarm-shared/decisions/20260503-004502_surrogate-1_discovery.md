# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Add `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs, and a training script that uses **CDN-only** fetches (zero HF API calls during training). This applies the CDN bypass pattern to avoid HF API rate limits while enabling Lightning Studio reuse.

### Steps (1h 45m total)

1. **Create tools/snapshot_manifest.py** (30m)  
   - Single `list_repo_tree` call for a date folder (e.g., `public-merged/2026-05-03`)  
   - Emit `file_manifest.json`: `{ "date": "...", "files": [ { "path": "...", "cdn_url": "...", "size": ... } ] }`  
   - Include repo, partition, and generation timestamp

2. **Create train_cdn.py** (45m)  
   - Load `file_manifest.json`  
   - Use `requests` to stream each CDN URL directly (no `datasets.load_dataset`)  
   - Parse each file to `{prompt, response}` only at parse time  
   - Yield examples for PyTorch DataLoader or HF Dataset from memory-mapped files  
   - Zero HF API calls during training loop  
   - Include retry logic (3 attempts with exponential backoff) for CDN resilience

3. **Create launcher notebook/script for Lightning Studio** (20m)  
   - Reuse running Studio if available (`Teamspace.studios` check)  
   - Upload `file_manifest.json` and `train_cdn.py` to Studio  
   - Start with `Machine.L40S` (or fallback to public tier)  
   - Handle idle-stop: check status before `.run()` and restart if stopped

4. **Update README** (10m)  
   - Add usage: `python tools/snapshot_manifest.py --date 2026-05-03 --repo axentx/surrogate-1-training-pairs`  
   - Document CDN-only training and Studio reuse

---

## tools/snapshot_manifest.py

```python
#!/usr/bin/env python3
"""
snapshot_manifest.py
List one date-partition of axentx/surrogate-1-training-pairs via a single
HF API call and emit file_manifest.json with CDN URLs.

Usage:
  python tools/snapshot_manifest.py --date 2026-05-03 --repo axentx/surrogate-1-training-pairs
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import huggingface_hub

HF_REPO = "axentx/surrogate-1-training-pairs"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_partition(repo: str, date: str) -> list[dict]:
    """
    Single API call: list top-level files under public-merged/<date>/
    """
    prefix = f"public-merged/{date}/"
    try:
        tree = huggingface_hub.list_repo_tree(
            repo=repo,
            path=prefix,
            recursive=False,
            repo_type="dataset",
        )
    except Exception as exc:
        print(f"ERROR listing repo tree for {prefix!r}: {exc}", file=sys.stderr)
        raise

    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        path = entry.path
        files.append(
            {
                "path": path,
                "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path),
                "size": getattr(entry, "size", None),
                "lfs": getattr(entry, "lfs", None),
            }
        )
    return files

def build_manifest(date: str, repo: str, output_path: Path) -> None:
    files = list_partition(repo=repo, date=date)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "date": date,
        "partition_prefix": f"public-merged/{date}/",
        "total_files": len(files),
        "files": files,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote manifest with {len(files)} files -> {output_path}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot HF partition manifest (CDN URLs).")
    parser.add_argument("--date", required=True, help="Date partition (YYYY-MM-DD)")
    parser.add_argument("--repo", default=HF_REPO, help="HF dataset repo")
    parser.add_argument(
        "--output",
        default="file_manifest.json",
        help="Output JSON path (default: file_manifest.json)",
    )
    args = parser.parse_args()

    # Optional: respect HF token for private repos; public datasets don't require auth for CDN.
    # If you hit 429 on API calls, wait 360s before retry.
    build_manifest(date=args.date, repo=args.repo, output_path=Path(args.output))

if __name__ == "__main__":
    main()
```

---

## train_cdn.py

```python
#!/usr/bin/env python3
"""
train_cdn.py
CDN-only training data loader for surrogate-1.
Uses file_manifest.json and streams files directly from CDN (no HF API calls).

Usage:
  python train_cdn.py --manifest file_manifest.json --batch-size 32
"""

import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import Iterator, Dict, Any

import numpy as np
import requests


def stream_cdn_lines(cdn_url: str, chunk_size: int = 8192, max_retries: int = 3) -> Iterator[str]:
    """
    Stream a JSONL file from CDN without auth.
    Yields lines incrementally to bound memory.
    Includes retry logic for CDN resilience.
    """
    for attempt in range(max_retries):
        try:
            with requests.get(cdn_url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                buffer = io.StringIO()
                for chunk in resp.iter_content(chunk_size=chunk_size, decode_unicode=True):
                    if not chunk:
                        continue
                    buffer.write(chunk)
                    buffer.seek(0)
                    # read complete lines
                    for line in buffer:
                        yield line.rstrip("\n\r")
                    buffer.seek(0)
                    buffer.truncate(0)
                # remainder
                remainder = buffer.getvalue().rstrip("\n\r")
                if remainder:
                    yield remainder
                return  # Success
        except Exception as exc:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"WARNING streaming {cdn_url} (attempt {attempt+1}/{max_retries}): {exc}. Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"ERROR streaming {cdn_url} after {max_retries} attempts: {exc}", file=sys.stderr)
                raise


def parse_record(line: str) -> Dict[str, Any] | None:
    """
    Project to {prompt, response} only.
    Accepts lines that are JSON objects with fields like:
      {"prompt": "...", "response": "...", ...}
    Returns None for malformed lines.
    """
    try:
        obj = json.loads(line)
        prompt = obj.get("prompt")
        response = obj.get("response")
        if prompt is None or response is None:
            return None
        return {"prompt": str(prompt), "response": str(response)}
    except Exception:
        return None


def iter_examples(manifest_path: Path) -> Iterator[Dict[str, Any]]:
    with manifest_path.open() as f:
        manifest = json.load(f)

    for file_info in manifest.get("files", []):
        cdn_url = file_info["cdn_url"]
        for line in stream_cdn_lines(cdn_url):
            rec = parse_record(line)
            if rec is not None:
                yield rec


def build_dataset(manifest_path: Path, limit: int | None = None) -> list[Dict[str,
