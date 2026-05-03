# vanguard / backend

## Final Synthesized Solution

**Core Diagnosis (unified):**  
The system lacks a CDN-first, content-addressed ingestion layer. Training and evaluation scripts still rely on runtime `list_repo_tree`/`load_dataset` calls, causing HF API 429s, non-reproducible runs, and schema drift on heterogeneous repos. There is no deterministic manifest keyed by date/slug, no pinning, and no guardrails for rate limits.

**Single Corrective Strategy:**  
Introduce an ingestion module that produces a pinned, CDN-only manifest per `(repo, date_folder)`, and update training to consume that manifest and fetch files exclusively via public CDN URLs. Avoid `load_dataset` for heterogeneous repos; use `hf_hub_download`/CDN with projection to `{prompt,response}` fields.

---

## 1. Implementation

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/backend/{ingest,train,manifests}
```

### backend/ingest/hf_cdn.py
```python
"""
HF CDN helpers: bypass API auth for public files and avoid rate limits.
"""
import os
import time
import json
import hashlib
import requests
from pathlib import Path
from typing import List, Dict, Optional
from huggingface_hub import list_repo_tree, HfApi

HF_CDN_ROOT = "https://huggingface.co/datasets"
MAX_RETRIES = 5
API_RATE_LIMIT_RESET_SECONDS = 360

def _backoff(attempt: int):
    sleep = (2 ** attempt) + (os.getpid() % 5)
    time.sleep(sleep)

def list_date_folder(repo_id: str, date_folder: str, token: Optional[str] = None) -> List[Dict]:
    """
    Non-recursive listing for a single date folder.
    Returns list of dicts with 'path' and 'type'.
    """
    api = HfApi(token=token)
    for attempt in range(MAX_RETRIES):
        try:
            tree = api.list_repo_tree(
                repo_id=repo_id,
                path=date_folder,
                recursive=False,
                token=token,
            )
            items = [{"path": f.rfilename, "type": f.type} for f in tree if f.type == "file"]
            return items
        except Exception as exc:
            resp_status = getattr(getattr(exc, "response", None), "status_code", None)
            if resp_status == 429:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(API_RATE_LIMIT_RESET_SECONDS)
                continue
            _backoff(attempt)
    raise RuntimeError(f"Failed to list {repo_id}/{date_folder}")

def cdn_url(repo_id: str, file_path: str) -> str:
    """Public CDN URL (no auth)."""
    return f"{HF_CDN_ROOT}/{repo_id}/resolve/main/{file_path}"

def download_cdn_file(url: str, dest_path: Path, chunk_size: int = 8192) -> Path:
    """Download via CDN with retries."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        f.write(chunk)
            return dest_path
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            _backoff(attempt)
    raise RuntimeError(f"Failed to download {url}")

def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
```

### backend/ingest/manifest.py
```python
"""
Generate CDN-first manifest for a repo + date folder.
Manifest is content-addressed and pinned.
"""
import json
import time
from pathlib import Path
from typing import List, Dict, Optional
from .hf_cdn import list_date_folder, cdn_url, download_cdn_file, hash_file

def build_manifest(
    repo_id: str,
    date_folder: str,
    out_dir: Path,
    token: Optional[str] = None,
    download: bool = False,
    limit: Optional[int] = None,
) -> Path:
    """
    Build manifest for repo_id/date_folder.

    Manifest schema:
    {
      "repo_id": "...",
      "date_folder": "...",
      "generated_at": "...",
      "files": [
        {
          "path": "...",
          "cdn_url": "...",
          "sha256": "...",
          "size": ...
        }
      ]
    }
    """
    items = list_date_folder(repo_id, date_folder, token=token)
    if limit is not None:
        items = items[:limit]

    files: List[Dict] = []
    for item in items:
        p = item["path"]
        url = cdn_url(repo_id, p)
        entry = {"path": p, "cdn_url": url, "sha256": None, "size": None}

        if download:
            tmp = Path("/tmp") / p.replace("/", "_")
            download_cdn_file(url, tmp)
            entry["sha256"] = hash_file(tmp)
            entry["size"] = tmp.stat().st_size
            tmp.unlink(missing_ok=True)

        files.append(entry)

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    slug = date_folder.strip("/").replace("/", "_") or "root"
    manifest_path = out_dir / f"{slug}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path
```

### backend/train/prepare_manifest.py
```python
#!/usr/bin/env python3
"""
Mac-side helper: pre-list a date folder and save manifest.
Run after HF API rate-limit window clears.
"""
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from ingest.manifest import build_manifest

def main():
    parser = argparse.ArgumentParser(description="Generate CDN-first manifest for training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo id")
    parser.add_argument("--date", required=True, help="Date folder path in repo")
    parser.add_argument("--out", default="manifests", help="Output directory (relative to repo root)")
    parser.add_argument("--token", default=None, help="HF token (optional for public repos)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of files (for testing)")
    args = parser.parse_args()

    out_dir = REPO_ROOT / args.out
    manifest_path = build_manifest(
        repo_id=args.repo,
        date_folder=args.date,
        out_dir=out_dir,
        token=args.token,
        download=False,
        limit=args.limit,
    )
    print(f"Manifest written: {manifest_path}")

if __name__ == "__main__":
    main()
```

### backend/train/train.py
```python
"""
Training entrypoint that consumes a CDN-first manifest and fetches via CDN only.
Avoids load_dataset for heterogeneous repos; projects to {prompt,response}.
"""
import json
import random
from pathlib import Path
from typing import Dict, List, Iterator, Any

import torch
from torch.utils.data import IterableDataset, DataLoader
import requests

from huggingface_hub import hf_hub_download

MANIFEST_PATH = Path(__file__).parent.parent / "manifests" / "your_manifest.json"

class CDNManifestDataset(IterableDataset):
    """
    Stream examples from a pinned manifest using CDN URLs.
    Each file is expected to be newline-delimited JSON with at least
    'prompt' and 'response' fields (heterogeneous-safe projection).
    """
    def __init__(
        self,

