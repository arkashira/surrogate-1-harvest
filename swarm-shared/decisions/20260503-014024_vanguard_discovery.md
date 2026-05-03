# vanguard / discovery

## Final Consolidated Solution

### 1. Diagnosis (merged)
- **Repeated authenticated enumeration**: Every training run or data-loader re-enumerates via `list_repo_tree`, burning HF API quota (1000/5min) and causing 429s.
- **No persisted manifest**: No `(repo, dateFolder) → file-list` artifact exists, preventing reuse across sessions, CI runs, and multi-node training.
- **Training still uses authenticated paths**: Code likely uses `load_dataset(streaming=True)` or repeated HF API calls during epoch loops instead of CDN-only fetches, amplifying rate-limit risk and introducing mixed-schema `CastError` hazards.
- **No graceful degradation**: Missing orchestration guard to fall back to CDN-only mode when API quota is exhausted.
- **No pre-materialization CLI**: No lightweight utility to produce a date-scoped manifest so Lightning training can run zero-API.

### 2. Proposed change (merged)
Add a focused discovery + loading utility that:
- Materializes a deterministic repo+date manifest once (JSON) using a single authenticated `list_repo_tree` walk.
- Embeds that manifest in training so subsequent runs use CDN-only downloads (`https://huggingface.co/datasets/.../resolve/main/...`) with zero API calls.
- Accepts `REPO` and `DATE_FOLDER` env vars and writes `manifests/{repo_safe}/{date_folder}.json`.
- Provides a CDN-first `IterableDataset` (with optional schema projection) and a small orchestration guard to keep Lightning Studio alive and avoid idle-stop loss.

Scope:
- New file: `/opt/axentx/vanguard/bin/materialize_manifest.py`
- Optional companion: `/opt/axentx/vanguard/train/loader_cdn.py` (or patch existing loader) to consume manifest and fetch via CDN.
- Optional orchestration helper: keepalive logic in training entrypoint to prevent idle-stop.

### 3. Implementation

```bash
mkdir -p /opt/axentx/vanguard/bin /opt/axentx/vanguard/manifests /opt/axentx/vanguard/train
```

#### `/opt/axentx/vanguard/bin/materialize_manifest.py`
```python
#!/usr/bin/env python3
"""
materialize_manifest.py
Produce a deterministic file-list manifest for (repo, date_folder)
to avoid repeated HF API list_repo_tree calls and prevent 429s.

Usage:
  REPO=datasets/myrepo DATE_FOLDER=2026-04-29 python3 materialize_manifest.py
"""
import os
import json
import hashlib
import sys
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: huggingface_hub not installed. pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

HF_API = HfApi()

def list_files_safe(repo_id: str, folder: str, max_files: int = 50_000):
    """
    Single authenticated list_repo_tree call per folder.
    Walk subfolders iteratively to avoid huge recursive pages.
    """
    all_files = []
    to_walk = [folder.rstrip("/")]

    while to_walk and len(all_files) < max_files:
        current = to_walk.pop(0)
        try:
            items = HF_API.list_repo_tree(repo_id=repo_id, path=current, recursive=False)
        except Exception as exc:
            print(f"WARN: failed to list {repo_id}/{current}: {exc}", file=sys.stderr)
            continue

        for item in items:
            # Normalize item access across HF Hub versions
            if hasattr(item, "path"):
                path = item.path
            elif hasattr(item, "rpath"):
                path = item.rpath
            else:
                path = str(item)

            if not path:
                continue

            is_dir = False
            if hasattr(item, "type"):
                is_dir = item.type == "directory"
            elif hasattr(item, "size") is False and path.endswith("/"):
                # heuristic fallback
                is_dir = True

            if is_dir:
                to_walk.append(path)
            else:
                # Only include likely data files (avoid huge checkpoints)
                if any(path.lower().endswith(ext) for ext in (".parquet", ".jsonl", ".json", ".csv", ".txt")):
                    all_files.append(path)

    return sorted(set(all_files))

def build_manifest(repo_id: str, date_folder: str):
    files = list_files_safe(repo_id, date_folder)
    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder.rstrip("/"),
        "files": files,
        "count": len(files),
        "sha256": "",
    }
    payload = json.dumps({"repo_id": repo_id, "date_folder": manifest["date_folder"], "files": files}, sort_keys=True).encode()
    manifest["sha256"] = hashlib.sha256(payload).hexdigest()
    return manifest

def main():
    repo_id = os.getenv("REPO")
    date_folder = os.getenv("DATE_FOLDER")

    if not repo_id or not date_folder:
        print("ERROR: set REPO and DATE_FOLDER env vars", file=sys.stderr)
        sys.exit(1)

    print(f"Materializing manifest for {repo_id}/{date_folder} ...")
    manifest = build_manifest(repo_id, date_folder)

    repo_safe = repo_id.replace("/", "_")
    out_dir = Path(__file__).parent.parent / "manifests" / repo_safe
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_folder.replace('/', '_')}.json"

    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {manifest['count']} files -> {out_path}")
    print(f"sha256:{manifest['sha256']}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/bin/materialize_manifest.py
```

#### `/opt/axentx/vanguard/train/loader_cdn.py`
```python
import json
import os
from pathlib import Path
from typing import Dict, Any, Iterator, List, Optional

import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset


class CDNParquetDataset(IterableDataset):
    """
    CDN-only Parquet loader using a pre-materialized manifest.
    Zero HF API calls after manifest creation.
    """

    def __init__(
        self,
        manifest_path: str,
        schema_project: Optional[Dict[str, str]] = None,
        max_retries: int = 3,
        timeout: int = 120,
    ):
        """
        Args:
            manifest_path: Path to manifest JSON produced by materialize_manifest.py
            schema_project: Optional mapping like {"prompt": "prompt", "response": "response"}
            max_retries: Per-file HTTP retries
            timeout: HTTP timeout per request
        """
        manifest = json.loads(Path(manifest_path).read_text())
        self.repo_id = manifest["repo_id"]
        self.files = [f for f in manifest["files"] if f.endswith(".parquet")]
        self.schema_project = schema_project or {}
        self.max_retries = max_retries
        self.timeout = timeout

        if not self.files:
            raise ValueError(f"No parquet files found in manifest {manifest_path}")

        # Build CDN URLs once
        self.urls = [
            f"https://huggingface.co/datasets/{self.repo_id}/resolve/main/{f}"
            for f in self.files
        ]

    def _stream_parquet(self, url: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, stream=True, timeout=self.timeout)
                resp.raise_for_status()
                # stream into memory; for very large files consider temp file + memory-map
                return resp.content
            except Exception as exc:
                if attempt == self.max_retries:
                    raise
                # simple backoff
                import time
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    def _parse_parquet(self, data: bytes) -> Iterator[Dict[str, Any]]:
        table = pq.read_table(io.BytesIO(data))
        for row in table.to_pylist():
            if self.schema_project:
                projected = {}
                for out_field, in_field in self.schema
