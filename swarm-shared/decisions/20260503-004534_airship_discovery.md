# airship / discovery

## Implementation Plan — Airship Discovery (CDN Manifest + Surrogate Training Fix)

**Highest-value incremental improvement (<2h):**  
Add a one-time manifest generator that lists dataset files via HF API once (respecting rate limits), then embed that manifest so Surrogate training uses **CDN-only fetches** (bypassing `/api/` during training) and projects only `{prompt, response}` at parse time to avoid `pyarrow.CastError`.

### Steps (≤2h)
1. Create `scripts/build_hf_cdn_manifest.py` (Mac/Linux orchestration) — 20m  
2. Add `surrogate/data/cdn_loader.py` (iterable dataset from CDN + projection) — 30m  
3. Wire into training entrypoint (`surrogate/train.py`) with fallback to local manifest — 20m  
4. Add cron-safe guard (shebang, executable, `SHELL=/bin/bash`) and usage doc — 15m  
5. Smoke test with small repo (10–20 files) — 15m  

---

## 1) Manifest generator (run from Mac)

File: `scripts/build_hf_cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate HF CDN manifest for Surrogate training.
Usage:
  bash scripts/build_hf_cdn_manifest.py \
    --repo huggingface/dataset-repo \
    --date-folder 2026-05-03 \
    --out surrogate/data/manifest_2026-05-03.json

Notes:
- Uses HF API once (respects 429/1000-per-5min).
- CDN URLs are https://huggingface.co/datasets/{repo}/resolve/main/{path}
- Only includes files under {date-folder}/ (non-recursive by default).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from huggingface_hub import HfApi, list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

API_RETRY_WAIT = 360  # seconds after 429

def build_manifest(repo: str, date_folder: str, out_path: str, recursive: bool = False):
    api = HfApi()
    prefix = date_folder.rstrip("/") + "/"
    print(f"Listing repo tree: {repo} prefix={prefix} recursive={recursive}")

    try:
        # list_repo_tree with recursive=False to avoid 100x pagination on huge repos
        entries = list_repo_tree(repo=repo, path=prefix, recursive=recursive)
    except Exception as e:
        if "429" in str(e):
            print(f"Rate limited 429. Waiting {API_RETRY_WAIT}s...")
            time.sleep(API_RETRY_WAIT)
            entries = list_repo_tree(repo=repo, path=prefix, recursive=recursive)
        else:
            raise

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        # CDN URL (no Authorization header required)
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{entry.path}"
        files.append({
            "path": entry.path,
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recursive": recursive,
        "files": files,
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {out} ({len(files)} files)")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF CDN manifest for training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/repo)")
    parser.add_argument("--date-folder", required=True, help="Folder prefix (e.g. 2026-05-03)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--recursive", action="store_true", help="List recursively")
    args = parser.parse_args()

    build_manifest(repo=args.repo, date_folder=args.date_folder, out_path=args.out, recursive=args.recursive)
```

Make executable:
```bash
chmod +x scripts/build_hf_cdn_manifest.py
```

---

## 2) CDN-only iterable loader (training side)

File: `surrogate/data/cdn_loader.py`

```python
import json
import logging
from pathlib import Path
from typing import Dict, Iterator, Optional

import requests

logger = logging.getLogger(__name__)

class CDNIterableDataset:
    """
    Iterable dataset that reads files from CDN URLs listed in a manifest.
    Projects each file to {prompt, response} at parse time to avoid pyarrow.CastError
    from heterogeneous schemas.

    Manifest format:
    {
      "repo": "...",
      "date_folder": "...",
      "files": [{"path": "...", "cdn_url": "...", "size": ...}, ...]
    }
    """

    def __init__(self, manifest_path: str, max_retries: int = 3, timeout: int = 30):
        self.manifest_path = Path(manifest_path)
        self.max_retries = max_retries
        self.timeout = timeout
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> Dict:
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _stream_file(self, cdn_url: str) -> Optional[str]:
        for attempt in range(1, self.max_retries + 1):
            try:
                with requests.get(cdn_url, stream=True, timeout=self.timeout) as r:
                    r.raise_for_status()
                    # streaming text read; adjust if binary parquet parsing is needed
                    return r.text
            except Exception as exc:
                logger.warning(f"Attempt {attempt}/{self.max_retries} failed for {cdn_url}: {exc}")
                if attempt == self.max_retries:
                    logger.error(f"Failed to fetch {cdn_url}")
                    return None
        return None

    def _project_to_pair(self, raw_text: str, file_path: str) -> Dict:
        """
        Project raw file to {prompt, response}.
        Customize per dataset layout. This is a minimal safe default:
        - If file is JSON/JSONL-like, try to extract fields.
        - Otherwise return raw as response with filename-derived prompt.
        """
        # Example: if file is .jsonl with {"prompt": "...", "response": "..."}
        # Implement dataset-specific projection here.
        # For safety, return a conservative projection:
        return {
            "prompt": f"file://{file_path}",
            "response": raw_text,
            "source_file": file_path,
        }

    def __iter__(self) -> Iterator[Dict]:
        files = self.manifest.get("files", [])
        logger.info(f"CDNIterableDataset iterating over {len(files)} files")
        for item in files:
            cdn_url = item.get("cdn_url")
            path = item.get("path")
            if not cdn_url or not path:
                continue

            raw = self._stream_file(cdn_url)
            if raw is None:
                continue

            try:
                yield self._project_to_pair(raw, path)
            except Exception as exc:
                logger.error(f"Projection failed for {path}: {exc}")
                continue
```

---

## 3) Wire into training script

File: `surrogate/train.py` (add/modify)

```python
import argparse
import logging
from pathlib import Path

from torch.utils.data import DataLoader

from surrogate.data.cdn_loader import CDNIterableDataset

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def build_dataloader(manifest_path: str, batch_size: int = 4, num_workers: int = 0):
    dataset = CDNIterableDataset(manifest_path=manifest_path)

