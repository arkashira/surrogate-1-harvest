# vanguard / backend

## Final Synthesis (Correctness + Actionability)

**Core Diagnosis (unified)**  
- No content-addressed manifest per date folder → training performs runtime `list_repo_tree`/`load_dataset` calls that trigger HF API 429s, non-reproducible epochs, and shard-order drift.  
- Data loader relies on Hugging Face `datasets` API during training instead of CDN-only fetches, wasting rate-limit quota and breaking determinism.  
- Missing deterministic file list embedded at launch → each epoch can see different shard ordering, newly arrived files, or pagination changes (non-reproducible runs).  
- No fallback when HF API is rate-limited or fails during training; job fails instead of switching to CDN-only mode.  
- Backend scripts invoke `load_dataset(..., streaming=True)` on heterogeneous repos (triggers pyarrow CastError and API churn).

---

## Final Implementation

### 1. Manifest generator  
`/opt/axentx/vanguard/backend/manifest.py`

```python
#!/usr/bin/env python3
"""
Generate content-addressed CDN-only manifest for one date folder.
Usage:
  python manifest.py --repo datasets/axentx/vanguard-mirror \
                     --date 2026-05-03 \
                     --out manifest.json
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

API = HfApi()
CDN_ROOT = "https://huggingface.co/datasets"

def list_date_folder(repo_id: str, date_folder: str):
    """
    Single non-recursive API call for one date folder.
    Returns list of dict: {path, size, sha}
    """
    try:
        entries = API.list_repo_tree(repo_id, path=date_folder, recursive=False)
    except Exception as exc:
        print(f"HF API error: {exc}", file=sys.stderr)
        return []

    out = []
    for e in entries:
        if isinstance(e, dict):
            path = e.get("path")
            size = e.get("size") or 0
            sha = e.get("sha") or ""
        else:
            path = str(e)
            size = 0
            sha = ""
        if path and path.endswith(".parquet"):
            out.append({"path": path, "size": size, "sha": sha})
    return out

def build_manifest(repo_id: str, date_folder: str):
    items = list_date_folder(repo_id, date_folder)
    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": [],
    }
    for item in items:
        url = f"{CDN_ROOT}/{repo_id}/resolve/main/{item['path']}"
        manifest["files"].append(
            {
                "local_path": item["path"],
                "url": url,
                "size": item["size"],
                "sha": item["sha"],
            }
        )

    # Deterministic ordering
    manifest["files"].sort(key=lambda x: x["local_path"])
    manifest["n_files"] = len(manifest["files"])
    manifest["total_size"] = sum(f["size"] for f in manifest["files"])

    # Content hash of manifest (excluding generated_at) for reproducibility
    reproducible = {k: v for k, v in manifest.items() if k != "generated_at"}
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(reproducible, sort_keys=True).encode()
    ).hexdigest()
    return manifest

def main():
    parser = argparse.ArgumentParser(description="Generate CDN-only manifest")
    parser.add_argument("--repo", default="datasets/axentx/vanguard-mirror")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    date_folder = f"batches/mirror-merged/{args.date}"
    manifest = build_manifest(args.repo, date_folder)

    Path(args.out).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest['files'])} files -> {args.out}")
    print(f"manifest_sha256: {manifest['manifest_sha256']}")

if __name__ == "__main__":
    main()
```

---

### 2. CDN-only training loader  
`/opt/axentx/vanguard/backend/train_cdn.py`

```python
"""
Lightning training script that uses CDN-only fetches.
Expects manifest.json in run directory (or passed via --manifest).
"""
import json
from pathlib import Path
from typing import Iterator

import fsspec
import pyarrow.parquet as pq
import torch
from torch.utils.data import IterableDataset

class CDNParquetIterable(IterableDataset):
    """
    Deterministic, CDN-only parquet stream.
    - Uses manifest.json for a fixed file list and ordering.
    - No Hugging Face datasets/list_repo_tree calls during training.
    - Per-worker sharding for multi-worker DataLoader compatibility.
    """

    def __init__(self, manifest_path: str):
        manifest_path = Path(manifest_path)
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.urls = [f["url"] for f in self.manifest["files"]]
        if not self.urls:
            raise ValueError("No parquet files found in manifest")

    def __iter__(self) -> Iterator[dict]:
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            indices = range(len(self.urls))
        else:
            per_worker = len(self.urls) // worker_info.num_workers
            worker_id = worker_info.id
            start = worker_id * per_worker
            end = (worker_id + 1) * per_worker if worker_id < worker_info.num_workers - 1 else len(self.urls)
            indices = range(start, end)

        for idx in indices:
            url = self.urls[idx]
            try:
                with fsspec.open(url, mode="rb", anon=True) as f:
                    table = pq.read_table(f)
                    if "prompt" in table.column_names and "response" in table.column_names:
                        for row in table.to_pylist():
                            yield {"prompt": row["prompt"], "response": row["response"]}
            except Exception as exc:
                # Log and continue; do not crash training on single corrupt/unavailable shard
                print(f"Failed to load {url}: {exc}")
                continue
```

---

### 3. Launcher / run script snippet

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/axentx/vanguard/backend

REPO="datasets/axentx/vanguard-mirror"
DATE="2026-05-03"
MANIFEST="manifest.json"

# 1) Generate deterministic manifest once per run
python manifest.py --repo "$REPO" --date "$DATE" --out "$MANIFEST"

# 2) Verify manifest integrity
python -c "
import json, hashlib, sys
m = json.load(open('$MANIFEST'))
assert m['n_files'] > 0, 'No files in manifest'
print('Files:', m['n_files'])
print('Total size:', m['total_size'])
print('SHA256:', m['manifest_sha256'])
"

# 3) Dry-run dataloader (no Lightning fit)
python -c "
from train_cdn import CDNParquetIterable
ds = CDNParquetIterable('$MANIFEST')
samples = [next(iter(ds)) for _ in range(5)]
for s in samples:
    assert 'prompt' in s and 'response' in s
print('Dry-run OK')
"

# 4) Start Lightning Studio / training with manifest available in run dir
# Example:
# lightning run model train.py --manifest "$MANIFEST"
```

---

## Verification Checklist

1. **Manifest generation**  
   ```bash
   cd /opt/axentx/vanguard/backend
   python manifest.py --repo datasets/axentx/vanguard-mirror --date 2026-05-03 --out manifest.json
   ```
   - Confirm `manifest.json
