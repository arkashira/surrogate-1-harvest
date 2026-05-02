# vanguard / backend

Below is the **single, consolidated solution**.  
It merges the strongest technical insights from both candidates, removes duplication, resolves contradictions in favor of **correctness + concrete actionability**, and provides a complete, ready-to-run implementation.

---

## 1. Unified Diagnosis (root causes)

| Issue | Risk | Fix |
|------|------|-----|
| Repeated `list_repo_tree` / `load_dataset` calls | HF API 429 (1000 req/5 min) | **Single manifest generation per folder** cached locally; training uses only CDN URLs |
| Lightning Studio not reused | Silent idle-stop kills runs; quota burn on create/stop cycles | **Detect running studio by name**; reuse if running; start only if stopped |
| `load_dataset(streaming=True)` on heterogeneous repos | PyArrow `CastError` from schema drift | **Project to `{prompt,response}` at parse time**; ignore extra fields; line-oriented JSONL fallback |
| All data via `/api/` endpoints | Counts against rate limits; slower | **CDN-only fetches** (`resolve/main/...`) during training; zero auth |
| Non-deterministic repo selection for writes | HF commit cap (128/hr/repo) can be hit by bursts | **Deterministic hash-based repo suffix** to spread write load across sibling repos |
| No retry/backoff on transient HF failures | Random job aborts | **Retry + exponential backoff** on CDN downloads and manifest fetch |

---

## 2. Proposed Change (summary)

- Add `/opt/axentx/vanguard/backend/training/file_manifest.py`  
  - One `list_repo_tree` call per folder → JSON manifest  
  - Deterministic sibling repo selector  
  - CDN URL builder  
- Update `/opt/axentx/vanguard/backend/training/train.py`  
  - Reuse running Lightning Studio by name  
  - `HFCDNDataset` using CDN URLs + local cache via `hf_hub_download`  
  - Projection to `{prompt,response}` at parse time (schema-agnostic)  
  - Retry/backoff and clear error handling  
- Add small helper `/opt/axentx/vanguard/backend/training/retry.py`  
  - Centralized retry policy for HF operations  

---

## 3. Implementation

### 3.1 `/opt/axentx/vanguard/backend/training/retry.py`

```python
#!/usr/bin/env python3
"""
Central retry utilities for HF operations.
"""
import time
import logging
from typing import Callable, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


def retry(
    fn: Callable[[], T],
    retries: int = 5,
    backoff: float = 1.0,
    factor: float = 2.0,
    allowed_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    attempt = 0
    delay = backoff
    while True:
        try:
            return fn()
        except allowed_exceptions as exc:
            attempt += 1
            if attempt > retries:
                logger.error(f"Retry exhausted for {fn.__name__}: {exc}")
                raise
            logger.warning(f"Retry {attempt}/{retries} for {fn.__name__}: {exc} -> sleep {delay:.1f}s")
            time.sleep(delay)
            delay *= factor
```

---

### 3.2 `/opt/axentx/vanguard/backend/training/file_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate and cache HF repo file manifests to avoid repeated API calls.

Usage:
  python file_manifest.py --repo datasets/my-corpus --folder 2026-05-02 --out manifest.json
"""
import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

from huggingface_hub import list_repo_tree

from .retry import retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def build_manifest(repo_id: str, folder: str, out_path: str) -> dict:
    """
    Single API call to list_repo_tree (non-recursive) for one folder.
    """
    logger.info("Listing %s/%s ...", repo_id, folder)

    tree = retry(
        lambda: list_repo_tree(repo_id=repo_id, path=folder, recursive=False),
        retries=5,
        backoff=2.0,
        allowed_exceptions=(Exception,),
    )

    entries = []
    for item in tree:
        if item.type == "file":
            entries.append(
                {
                    "path": item.path,
                    "size": getattr(item, "size", None),
                    "lfs": getattr(item, "lfs", None),
                }
            )

    manifest = {
        "repo_id": repo_id,
        "folder": folder,
        "generated_by": "file_manifest.py",
        "entries": entries,
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    logger.info("Wrote %d entries to %s", len(entries), out)
    return manifest


def pick_sibling_repo(slug: str, n_siblings: int = 5) -> str:
    """
    Deterministic sibling repo selection to spread HF commit load.
    Returns repo_id suffix like '-s0' ... '-s{n-1}'.
    """
    digest = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = digest % n_siblings
    return f"-s{idx}" if idx > 0 else ""


def cdn_url(repo_id: str, path: str) -> str:
    """CDN URL that bypasses HF API auth/rate limits."""
    return f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF file manifest.")
    parser.add_argument("--repo", required=True, help="HF repo id (e.g. datasets/my-corpus)")
    parser.add_argument("--folder", required=True, help="Folder to list (non-recursive)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.repo, args.folder, args.out)
```

---

### 3.3 `/opt/axentx/vanguard/backend/training/train.py`

```python
#!/usr/bin/env python3
"""
Train surrogate-1 with CDN-only fetches and Lightning Studio reuse.
Expects a manifest.json produced by file_manifest.py in the same folder.
"""
import json
import logging
import os
import sys
from pathlib import Path

import torch
from torch.utils.data import IterableDataset, DataLoader
import lightning as L
from lightning.pytorch.cli import LightningCLI

from huggingface_hub import hf_hub_download

from .retry import retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class HFCDNDataset(IterableDataset):
    """
    Uses CDN URLs (no auth) and projects to {prompt,response} at parse time.
    Avoids load_dataset(streaming=True) on heterogeneous repos.
    """

    def __init__(self, manifest_path: str, repo_id: str, max_files: int = -1):
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")
        with open(manifest_path) as f:
            manifest = json.load(f)

        entries = manifest["entries"]
        if max_files > 0:
            entries = entries[:max_files]

        self.repo_id = repo_id
        self.paths = []
        for e in entries:
            p = retry(
                lambda ep=e: hf_hub_download(
                    repo_id=repo_id,
                    filename=ep["path"],
                    repo_type="dataset",
                    local_files_only=False,
                ),
                retries=3,
                backoff=2.0,
                allowed_exceptions=(Exception,),
            )
            self.paths.append(p)

    def __iter__(self):
        for p in self.paths:
            try:
                with open(p, "r
