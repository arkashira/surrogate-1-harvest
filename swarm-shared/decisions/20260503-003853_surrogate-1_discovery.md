# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs + integrity metadata, and patch Lightning training to use **zero-API CDN-only** data loading (bypasses 429 rate limits during training).

### Why this wins
- Eliminates HF API rate-limit risk during long training runs (CDN unlimited vs 1k/5min API limit)
- Single Mac-side API call fits within free tier and respects HF commit cap
- Enables deterministic, reproducible training inputs via manifest
- Reuses existing patterns: pre-list once → embed → CDN-only workers

---

### 1. Create `tools/snapshot_manifest.py`

```python
#!/usr/bin/env python3
"""
snapshot_manifest.py
Mac-side tool: list one date-partition of axentx/surrogate-1-training-pairs
and emit file_manifest.json with CDN URLs + integrity metadata.

Usage:
  python tools/snapshot_manifest.py --date 2026-04-30 --out file_manifest.json
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download

API_REPO = "datasets/axentx/surrogate-1-training-pairs"
CDN_ROOT = "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main"

def list_partition(date: str) -> list[dict]:
    """
    Single API call: list files under <date>/ (non-recursive by folder).
    Returns list of dicts with cdn_url, size, etag-friendly metadata.
    """
    api = HfApi()
    prefix = f"{date}/"
    try:
        # One paginated call for the folder only
        entries = api.list_repo_tree(
            repo_id=API_REPO,
            path=prefix,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"HF API error listing {prefix}: {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        cdn_url = f"{CDN_ROOT}/{entry.path}"
        files.append({
            "path": entry.path,
            "cdn_url": cdn_url,
            "size": entry.size,
            "last_modified": getattr(entry, "last_modified", None),
        })
    return files

def build_manifest(date: str, output_path: Path) -> None:
    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date_partition": date,
        "repo": API_REPO,
        "strategy": "cdn-only",
        "files": list_partition(date),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"Wrote {len(manifest['files'])} files to {output_path}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot HF dataset partition manifest")
    parser.add_argument("--date", required=True, help="Date partition (YYYY-MM-DD)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()

    build_manifest(date=args.date, output_path=Path(args.out))

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x tools/snapshot_manifest.py
```

---

### 2. Add `tools/requirements.txt` (lightweight)

```
huggingface_hub>=0.22.0
requests>=2.31.0
```

---

### 3. Patch Lightning training script to use CDN-only manifest

Create/modify `train.py` (or your existing training entrypoint) to accept manifest and use `requests` streaming + `datasets` `load_from_disk` or direct parquet read via CDN URLs.

```python
#!/usr/bin/env python3
"""
train.py
Lightning Studio training entrypoint.
Uses file_manifest.json for CDN-only data loading (zero HF API calls during training).
"""

import json
import os
import tempfile
from pathlib import Path

import pyarrow.parquet as pq
import requests
import torch
from datasets import Dataset, DatasetDict
from torch.utils.data import DataLoader

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "file_manifest.json")
CACHE_DIR = Path(os.getenv("HF_HOME", "~/.cache/hf_cdn")).expanduser()
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def download_via_cdn(cdn_url: str, cache_path: Path) -> Path:
    """Download file via CDN (no auth header) with retries."""
    if cache_path.exists():
        return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            with requests.get(cdn_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(cache_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return cache_path
        except Exception as e:
            if attempt == 2:
                raise
            print(f"Retry {attempt+1}/3 for {cdn_url}: {e}")
    raise RuntimeError(f"Failed to download {cdn_url}")

def load_dataset_from_manifest(manifest_path: str) -> Dataset:
    """Load dataset using CDN URLs only (zero HF API calls)."""
    with open(manifest_path) as f:
        manifest = json.load(f)

    rows = []
    for file_info in manifest["files"]:
        cdn_url = file_info["cdn_url"]
        fname = Path(file_info["path"]).name
        cache_path = CACHE_DIR / fname
        local_path = download_via_cdn(cdn_url, cache_path)

        # Project only {prompt, response} at parse time (handles mixed schemas)
        try:
            table = pq.read_table(local_path, columns=["prompt", "response"])
        except Exception:
            # Fallback: read all and select
            table = pq.read_table(local_path)
            if "prompt" not in table.column_names or "response" not in table.column_names:
                continue
            table = table.select(["prompt", "response"])

        batch = table.to_pylist()
        rows.extend(batch)

    return Dataset.from_list(rows)

def main() -> None:
    # Reuse running studio if available (saves quota)
    try:
        from lightning.pytorch import Trainer
        from lightning.pytorch.loggers import CSVLogger
    except ImportError:
        print("Lightning not installed; skipping trainer setup")
        return

    print("Loading dataset via CDN-only manifest...")
    dataset = load_dataset_from_manifest(MANIFEST_PATH)
    dataset = dataset.train_test_split(test_size=0.05)

    # Minimal example model/train loop — replace with your actual model
    from torch.utils.data import DataLoader

    train_loader = DataLoader(dataset["train"], batch_size=8, shuffle=True)

    # Dummy training step placeholder
    model = torch.nn.Linear(1024, 1)  # replace with real model
    trainer = Trainer(
        max_epochs=1,
        logger=CSVLogger("logs", "run"),
        accelerator="gpu" if torch.cuda.is_available() else "cpu",
    )
    print("Starting training (CDN-only data path)...")
    # trainer.fit(model, train_loader)  # uncomment with real model/data

if __name__ == "__main__":
    main()
```

---

### 4. Add run script for Mac orchestration

`tools/run_snapshot_and_train.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Mac orchestration script: snapshot manifest → Lightning training (CDN-only)
# Ensures no HF API calls during training (bypasses 429 rate limits).

export HF_HOME="${HF_HOME:-$HOME/.cache/hf}"
export MANIFEST_PATH="${MANIFEST_PATH:-file_manifest.json}"
DATE="${DATE:-$(date -u +
