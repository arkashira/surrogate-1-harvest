# airship / frontend

## Highest-Value Incremental Improvement (<2h)

**Problem**: Surrogate-1 training blocked by HF API 429s during dataset loading; training stalls waiting on API pagination instead of GPU.

**Fix**: Implement CDN-only data loading with a pre-computed file manifest. One-time Mac-side `list_repo_tree` → JSON manifest → Lightning training uses only CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with zero API calls during training.

**Why this wins**:
- Eliminates 429s during training (CDN tier >> API tier)
- Preserves Lightning quota (studio reuse + no API churn)
- <2h implementation (single manifest + small loader patch)
- Enables continuous retraining without babysitting rate limits

---

## Implementation Plan (≤2h)

### 1. Generate Manifest (Mac orchestration, ~5 min)
```bash
# From /opt/axentx/airship
cd /opt/axentx/airship
python scripts/generate_manifest.py \
  --repo "huggingface-dataset-org/surrogate-1-data" \
  --date-folder "2026-05-02" \
  --out "data/manifest-2026-05-02.json"
```

### 2. Add CDN-Only Data Loader (~45 min)
File: `/opt/axentx/airship/surrogate/data/cdn_dataset.py`

```python
import json
import os
from pathlib import Path
from typing import List, Dict

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset
from tqdm import tqdm

HF_CDN = "https://huggingface.co/datasets"

class CDNParquetDataset(IterableDataset):
    """
    CDN-only Parquet loader for Surrogate-1 training.
    Zero HuggingFace API calls during training.
    """
    def __init__(self, manifest_path: str, repo: str, columns: List[str] = None):
        self.repo = repo
        self.columns = columns or ["prompt", "response"]
        with open(manifest_path) as f:
            self.files = json.load(f)["parquet_files"]
        self._validate()

    def _validate(self):
        if not self.files:
            raise ValueError("No parquet files in manifest")

    def _cdn_url(self, file_path: str) -> str:
        # file_path relative to dataset root, e.g. "2026-05-02/batch-001.parquet"
        return f"{HF_CDN}/{self.repo}/resolve/main/{file_path}"

    def _stream_parquet(self, url: str):
        # CDN download -> bytes -> pyarrow (no HF datasets)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return pq.read_table(pa.BufferReader(resp.content), columns=self.columns)

    def __iter__(self):
        for file_path in self.files:
            url = self._cdn_url(file_path)
            try:
                table = self._stream_parquet(url)
                for i in range(table.num_rows):
                    row = {
                        col: table[col][i].as_py()
                        for col in self.columns
                        if col in table.column_names
                    }
                    # Normalize to {prompt, response}
                    if "prompt" not in row or "response" not in row:
                        continue
                    yield row
            except Exception as exc:
                # Log and skip bad shards; don't kill training
                print(f"Skipping {url}: {exc}")
                continue
```

### 3. Training Script Patch (~30 min)
File: `/opt/axentx/airship/surrogate/train.py` (or equivalent)

```python
# Before:
# from datasets import load_dataset
# dataset = load_dataset("huggingface-dataset-org/surrogate-1-data", streaming=True)

# After:
from surrogate.data.cdn_dataset import CDNParquetDataset

manifest = "data/manifest-2026-05-02.json"
repo = "huggingface-dataset-org/surrogate-1-data"

train_dataset = CDNParquetDataset(
    manifest_path=manifest,
    repo=repo,
    columns=["prompt", "response"]
)

# Wrap with torch DataLoader as usual
from torch.utils.data import DataLoader
train_loader = DataLoader(
    train_dataset,
    batch_size=8,
    num_workers=0,  # avoid fork issues with requests
    pin_memory=True
)
```

### 4. Manifest Generator (~30 min)
File: `/opt/axentx/airship/scripts/generate_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate CDN manifest for Surrogate-1 training.
Run on Mac (or any orchestration node) after rate-limit window clears.
"""
import argparse
import json
from datetime import datetime

from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date-folder", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    # Single non-recursive call per date folder (avoids 100x pagination)
    tree = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date_folder,
        recursive=False
    )

    parquet_files = [
        f.rfilename for f in tree
        if f.rfilename.endswith(".parquet")
    ]

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "repo": args.repo,
        "date_folder": args.date_folder,
        "parquet_files": sorted(parquet_files)
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {args.out} ({len(parquet_files)} files)")

if __name__ == "__main__":
    main()
```

### 5. Lightning Studio Reuse Guard (~15 min)
File: `/opt/axentx/airship/surrogate/launch_studio.py`

```python
from lightning import LightningWork, LightningFlow, LightningApp
from lightning.app import BuildConfig

def get_or_create_studio(name: str, machine: str = "L40S"):
    from lightning import Teamspace
    for s in Teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Teamspace.studios.create(
        name=name,
        machine=machine,
        # Avoid recreating on every run → saves 80hr/mo quota
        create_ok=True
    )
```

### 6. Smoke Test (~15 min)
```bash
# 1) Generate manifest
python scripts/generate_manifest.py \
  --repo "huggingface-dataset-org/surrogate-1-data" \
  --date-folder "2026-05-02" \
  --out "data/manifest-2026-05-02.json"

# 2) Single-batch test
python -c "
from surrogate.data.cdn_dataset import CDNParquetDataset
ds = CDNParquetDataset('data/manifest-2026-05-02.json', 'huggingface-dataset-org/surrogate-1-data')
for i, row in enumerate(ds):
    print(i, row['prompt'][:80])
    if i >= 2: break
"

# 3) Launch training (Lightning)
python train.py --manifest data/manifest-2026-05-02.json
```

---

## Verification Checklist

- [ ] Manifest contains expected parquet files for target date
- [ ] `CDNParquetDataset` yields `{prompt, response}` dicts
- [ ] Training loop runs ≥100 steps without HF API calls (check logs for 429s)
- [ ] Studio reused if already running (saves quota)
- [ ] No `load_dataset(streaming=True)` in active training code

---

## Rollback Plan

If CDN fetch issues arise:
1. Revert `train.py
