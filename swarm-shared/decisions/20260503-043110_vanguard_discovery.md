# vanguard / discovery

# Final Synthesis

## 1. Unified Diagnosis (Accepted)
- **No content-addressed manifest per date folder** → forces runtime repo re-enumeration and triggers HF API 429s.
- **No deterministic `{path, sha256}` snapshot** → epochs are non-reproducible; CDN-only fetches cannot be validated and resume/restart is unsafe.
- **Reliance on `load_dataset`/`list_repo_files` (recursive)** → high API pressure, no offline/CDN-only training path.
- **Missing lightweight orchestration wrapper** to generate manifests on Mac/CI and embed them in Lightning Studio runs.
- **No fallback when HF API is rate-limited** → training stalls instead of falling back to CDN-only mode.

## 2. Unified Proposed Change
Add a single-file manifest generator + CDN-only dataset reader:
- **`vanguard/discovery/manifest.py`** — CLI: `python manifest.py build --repo <repo> --date <YYYY-MM-DD> --out <path>`
  - Uses `list_repo_tree(..., recursive=False)` per date folder (paginated, non-recursive).
  - Produces `manifest-<date>.jsonl`: `{"path": "...", "sha256": "...", "cdn_url": "...", "size": ...}`.
  - Stores manifests under `manifests/`.
- **`vanguard/discovery/dataset.py`** — `CdnDataset` class:
  - Loads manifest JSONL.
  - Streams files via `requests.get(cdn_url, timeout=60)` with retries and optional integrity checks.
  - Projects to `{prompt, response}` at parse time (avoids pyarrow schema issues).
  - Supports shuffle, resume, deterministic epoch ordering via seed, and optional validation of `sha256`.
- **Update training launcher** to accept `--manifest` and use `CdnDataset` instead of `load_dataset`.

## 3. Final Implementation

```bash
# /opt/axentx/vanguard/discovery/manifest.py
#!/usr/bin/env python3
"""
Build a content-addressed manifest for a HuggingFace dataset repo
for a single date folder. Avoids recursive list_repo_files and enables
CDN-only fetches during training.

Usage:
  python manifest.py build --repo datasets/mycorp/vanguard-ingest --date 2026-04-29 --out manifests/
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import requests
from huggingface_hub import HfApi

API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
RATE_LIMIT_RESET_WAIT = 360

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def list_date_files(repo: str, date: str, token: str = None) -> List[Dict]:
    """
    Non-recursive per-folder listing for repo/datasets/{date}/*
    Returns list[dict] with keys: path, size (if available)
    """
    folder = f"datasets/{date}"
    try:
        items = API.list_repo_tree(repo=repo, path=folder, recursive=False, token=token)
    except Exception as e:
        if "429" in str(e):
            print(f"[WARN] HF API 429, waiting {RATE_LIMIT_RESET_WAIT}s", file=sys.stderr)
            time.sleep(RATE_LIMIT_RESET_WAIT)
            items = API.list_repo_tree(repo=repo, path=folder, recursive=False, token=token)
        else:
            raise

    files = []
    for it in items:
        if it.get("type") == "file":
            files.append({"path": it["path"], "size": it.get("size")})
    return files

def build_manifest(
    repo: str,
    date: str,
    out_dir: str,
    token: str = None,
    chunk_size: int = 8192,
    skip_sha: bool = False
) -> str:
    out_path = Path(out_dir) / f"manifest-{date}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files = list_date_files(repo, date, token=token)
    print(f"[INFO] Found {len(files)} files for {repo}@{date}")

    with open(out_path, "w", encoding="utf-8") as f:
        for item in files:
            path = item["path"]
            cdn_url = CDN_TEMPLATE.format(repo=repo, path=path)
            sha = None
            if not skip_sha:
                try:
                    resp = requests.get(cdn_url, timeout=60, stream=True)
                    resp.raise_for_status()
                    h = hashlib.sha256()
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        h.update(chunk)
                    sha = h.hexdigest()
                except Exception as e:
                    print(f"[WARN] Could not fetch {cdn_url}: {e}", file=sys.stderr)

            rec = {
                "path": path,
                "sha256": sha,
                "cdn_url": cdn_url,
                "size": item.get("size"),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[DONE] Manifest written to {out_path}")
    return str(out_path)

def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN manifest for a HF dataset repo+date")
    parser.add_argument("action", choices=["build"], help="Action to perform")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/mycorp/vanguard-ingest)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="manifests", help="Output directory for manifest JSONL")
    parser.add_argument("--token", default=None, help="HF token (optional; CDN downloads do not require auth)")
    parser.add_argument("--skip-sha", action="store_true", help="Skip SHA256 computation (faster, less safe)")
    args = parser.parse_args()

    if args.action == "build":
        build_manifest(args.repo, args.date, args.out, token=args.token, skip_sha=args.skip_sha)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/discovery/dataset.py
"""
CDN-only dataset reader for HF repos using a content-addressed manifest.
Avoids HF API calls during training; uses direct CDN downloads.
"""

import hashlib
import json
import random
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Callable

import requests
from tqdm import tqdm

class CdnDataset:
    """
    Lightweight streaming dataset backed by a manifest JSONL:
      {"path": "...", "sha256": "...", "cdn_url": "...", "size": ...}

    Features:
    - Deterministic ordering when seed is set.
    - Resume from offset.
    - Optional SHA256 integrity checks.
    - Projects to {prompt, response} at parse time (avoids pyarrow schema issues).
    """

    def __init__(
        self,
        manifest_path: str,
        seed: Optional[int] = None,
        shuffle: bool = False,
        validate_sha: bool = True,
        projection_fn: Optional[Callable[[bytes], Dict]] = None,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        timeout: int = 60,
    ):
        self.manifest_path = Path(manifest_path)
        self.records: List[Dict] = self._load_manifest()
        self.seed = seed
        self.shuffle = shuffle
        self.validate_sha = validate_sha
        self.projection_fn = projection_fn or self._default_projection
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout

        self._rng = random.Random(seed)
        if self.shuffle:
            self._rng.shuffle(self.records)

    def _load_manifest(self) -> List[Dict]:
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {self.manifest_path}")
        records = []
        with open(self
