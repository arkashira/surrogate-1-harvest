# vanguard / quality

### 1. Diagnosis (consolidated)
- **No persisted `(repo, dateFolder)` manifest** → repeated authenticated `list_repo_tree` calls burn HF API quota and trigger 429s.  
- **Training/ingestion uses `load_dataset(streaming=True)` on heterogeneous repos** → `pyarrow.CastError` from mixed schemas.  
- **Data fetches use authenticated `/api/` paths instead of public CDN URLs** → avoidable rate-limit pressure.  
- **No client-side or backend caching of file lists** → frontend and training re-enumerate HF API unnecessarily.  
- **Missing deterministic repo selection and burst handling** → HF commit cap (128/hr/repo) can block ingestion bursts.

### 2. Proposed change (single coherent plan)
Add a lightweight persisted manifest generator and CDN-only fetcher for the backend ingestion/training path:
- Create `/opt/axentx/vanguard/backend/manifest.py` (new) to build and cache a non-recursive `(repo, dateFolder)` manifest and expose CDN URL helpers.  
- Modify `/opt/axentx/vanguard/backend/train.py` to:
  - Load the persisted manifest instead of calling HF API repeatedly.  
  - Use a `HFCDNDataset` that streams rows from CDN-hosted Parquet files (no `load_dataset`, no auth headers).  
  - Handle mixed schemas safely (per-file schema enforcement + safe casting).  
- Keep manifests date-folder-scoped JSON files; embed manifest usage in training so data loading makes zero API calls.

### 3. Implementation

#### `/opt/axentx/vanguard/backend/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate and persist a non-recursive (repo, dateFolder) manifest.
Usage:
    python manifest.py --repo datasets/myrepo --date 2026-04-29 --out manifest.json
"""
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except Exception:  # pragma: no cover
    list_repo_tree = None

HF_API_RATE_LIMIT_RESET_BUFFER = 360  # seconds


def build_manifest(repo: str, date_folder: str, out_path: str, retry_wait: int = 30) -> dict:
    """
    Single API call: list_repo_tree(path=date_folder, recursive=False).
    Persist JSON: { "repo": "...", "date_folder": "...", "files": [...], "ts": ... }
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not available")

    attempt = 0
    while True:
        try:
            tree = list_repo_tree(repo_id=repo, path=date_folder, recursive=False)
            files = [item.rfilename for item in tree if item.rfilename]
            manifest = {
                "repo": repo,
                "date_folder": date_folder,
                "files": sorted(files),
                "ts": datetime.utcnow().isoformat() + "Z",
            }
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            return manifest
        except Exception as exc:
            # crude 429 detection; prefer parsing Retry-After when available
            if "429" in str(exc) or "rate limit" in str(exc).lower():
                attempt += 1
                wait = max(retry_wait, HF_API_RATE_LIMIT_RESET_BUFFER)
                print(f"Rate limited (attempt {attempt}). Waiting {wait}s: {exc}")
                time.sleep(wait)
                continue
            raise


def cdn_url(repo: str, file_path: str) -> str:
    """Public CDN URL — no Authorization header required."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"


def load_manifest(manifest_path: str) -> dict:
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF date-folder manifest")
    parser.add_argument("--repo", required=True, help="HF dataset repo id")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()
    manifest = build_manifest(args.repo, args.date, args.out)
    print(f"Wrote {len(manifest['files'])} files to {args.out}")
```

#### `/opt/axentx/vanguard/backend/train.py` (key changes)
```python
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from torch.utils.data import Dataset, DataLoader

from manifest import cdn_url, load_manifest


def _normalize_batch(batch: Dict[str, np.ndarray], schema: pa.Schema) -> Dict[str, np.ndarray]:
    """Coerce batch arrays to schema types to avoid pyarrow.CastError on mixed schemas."""
    out: Dict[str, np.ndarray] = {}
    for name in schema.names:
        if name not in batch:
            # Missing column -> null array matching length of first available column
            length = next((len(v) for v in batch.values()), 0)
            out[name] = np.full(length, None, dtype=object)
            continue

        arr = batch[name]
        target_type = schema.field(name).type

        # Basic safe conversions
        try:
            if pa.types.is_string(target_type):
                out[name] = np.array(arr, dtype=object)
            elif pa.types.is_integer(target_type):
                out[name] = np.array(arr, dtype=np.int64)
            elif pa.types.is_floating(target_type):
                out[name] = np.array(arr, dtype=np.float64)
            elif pa.types.is_boolean(target_type):
                out[name] = np.array(arr, dtype=bool)
            else:
                out[name] = np.array(arr, dtype=object)
        except Exception:
            # Fallback: object dtype to keep pipeline running
            out[name] = np.array(arr, dtype=object)
    return out


class HFCDNDataset(Dataset):
    """
    Streams rows from CDN-hosted Parquet files listed in a persisted manifest.
    Avoids load_dataset() and authenticated API calls during training.
    """

    def __init__(self, manifest_path: str, max_files: Optional[int] = None):
        self.manifest = load_manifest(manifest_path)
        self.repo = self.manifest["repo"]
        self.files = [f for f in self.manifest["files"] if f.endswith(".parquet")]
        if max_files is not None:
            self.files = self.files[:max_files]

        if not self.files:
            raise ValueError("No Parquet files found in manifest")

        # Prefer schema from first file; used for safe casting across files
        first_path = self.files[0]
        with _open_cdn_parquet(self.repo, first_path) as pf:
            self.schema = pf.schema

        self.file_idx = 0
        self.current_batch: List[Dict[str, Any]] = []
        self.current_reader: Optional[pq.ParquetFile] = None
        self._ensure_reader()

    def _ensure_reader(self) -> None:
        if self.current_reader is not None:
            return
        while self.file_idx < len(self.files):
            try:
                self.current_reader = _open_cdn_parquet(self.repo, self.files[self.file_idx])
                self.file_idx += 1
                return
            except Exception as exc:
                # Skip corrupt/unreadable file and continue
                print(f"Skipping {self.files[self.file_idx - 1]}: {exc}")
                self.current_reader = None
        # No readable files left
        self.current_reader = None

    def __len__(self) -> int:
        # Approximate; exact row count would require metadata reads
        return len(self.files) * 10_000

    def __getitem__(self, idx: int) -> Dict[str, Any]:
        # Simple row-at-a-time iterator for DataLoader compatibility.
        # For performance, consider batch reads via _iter_batches.
        while True:
            if self.current_batch
