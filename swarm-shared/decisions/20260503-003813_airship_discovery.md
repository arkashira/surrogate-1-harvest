# airship / discovery

## Final Implementation — Manifest-Driven CDN-Only Dataset Loader

**Goal**: Eliminate HF API rate limits and mixed-schema ingestion failures by replacing `load_dataset`/`list_repo_files` with a manifest-driven, CDN-only iterable loader for surrogate training.  
**Estimated effort**: 1.5–2h

---

### 1) One-time manifest generator (run on Mac)

`scripts/build_dataset_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a date-scoped manifest of parquet files for CDN-only training.
Usage:
  HF_REPO=datasets/your/repo python scripts/build_dataset_manifest.py \
    --date-folder 2026-05-03 \
    --out manifest-2026-05-03.json
"""
import os
import json
import argparse
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_REPO", "datasets/your/repo")
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

def build_manifest(date_folder: str, out_path: str):
    api = HfApi()
    # Single API call: list one folder only (no recursive)
    entries = api.list_repo_tree(
        repo_id=HF_REPO,
        path=f"batches/mirror-merged/{date_folder}",
        repo_type="dataset",
        recursive=False,
    )
    files = sorted(
        e.rfilename for e in entries
        if e.rfilename.endswith(".parquet") and not e.rfilename.endswith("/")
    )

    manifest = {
        "repo": HF_REPO,
        "date_folder": date_folder,
        "cdn_root": CDN_ROOT,
        "files": [
            {
                "filename": f,
                "cdn_url": f"{CDN_ROOT}/batches/mirror-merged/{date_folder}/{f}",
            }
            for f in files
        ],
    }

    with open(out_path, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-folder", required=True)
    parser.add_argument("--out", default="dataset_manifest.json")
    args = parser.parse_args()
    build_manifest(args.date_folder, args.out)
```

Make executable:

```bash
chmod +x scripts/build_dataset_manifest.py
```

**Notes**:
- Do not embed `hf_hub_download` in the manifest generator; keep manifest pure CDN URLs to avoid accidental quota use and to allow cross-machine reuse.
- Manifest is small and deterministic; commit to repo.

---

### 2) CDN-only iterable dataset loader

`surrogate/data/cdn_parquet_loader.py`

```python
from __future__ import annotations

import io
import json
from typing import Dict, Any, Iterator, List
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from dataclasses import dataclass


@dataclass
class CDNParquetShard:
    cdn_url: str
    local_path: str | None = None


class CDNParquetIterable:
    """
    Iterable over parquet shards fetched via CDN (no HF API during training).
    Yields {"prompt": ..., "response": ...} dicts. Projection to columns happens
    at parse time to avoid mixed-schema ingestion failures.
    """

    def __init__(self, manifest_path: str, batch_size: int = 1000):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.shards: List[CDNParquetShard] = [
            CDNParquetShard(cdn_url=item["cdn_url"], local_path=item.get("local_path"))
            for item in self.manifest["files"]
        ]
        self.batch_size = batch_size

    def _read_shard_local(self, shard: CDNParquetShard) -> Iterator[Dict[str, Any]]:
        table = pq.read_table(shard.local_path, columns=["prompt", "response"])
        for batch in table.to_batches(max_chunksize=self.batch_size):
            df = batch.to_pydict()
            for prompt, response in zip(df["prompt"], df["response"]):
                yield {"prompt": prompt, "response": response}

    def _read_shard_cdn(self, shard: CDNParquetShard) -> Iterator[Dict[str, Any]]:
        # Streaming download via CDN (no auth, no API rate limit)
        resp = requests.get(shard.cdn_url, stream=True, timeout=120)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        table = pq.read_table(buf, columns=["prompt", "response"])
        for batch in table.to_batches(max_chunksize=self.batch_size):
            df = batch.to_pydict()
            for prompt, response in zip(df["prompt"], df["response"]):
                yield {"prompt": prompt, "response": response}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for shard in self.shards:
            try:
                if shard.local_path:
                    yield from self._read_shard_local(shard)
                else:
                    yield from self._read_shard_cdn(shard)
            except Exception as exc:
                # Never fail entire epoch on one shard
                print(f"Skipping shard {shard.cdn_url}: {exc}")
                continue
```

---

### 3) Lightning-compatible DataModule

`surrogate/data/cdn_datamodule.py`

```python
from torch.utils.data import IterableDataset, DataLoader
from surrogate.data.cdn_parquet_loader import CDNParquetIterable


class CDNParquetIterableDataset(IterableDataset):
    def __init__(self, manifest_path: str):
        self.manifest_path = manifest_path

    def __iter__(self):
        return CDNParquetIterable(self.manifest_path)


class CDNDataModule:
    def __init__(self, manifest_path: str, batch_size: int = 8, num_workers: int = 2):
        self.manifest_path = manifest_path
        self.batch_size = batch_size
        self.num_workers = num_workers

    def train_dataloader(self):
        dataset = CDNParquetIterableDataset(self.manifest_path)
        return DataLoader(
            dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
        )
```

---

### 4) Update surrogate training launcher

`surrogate/train.py` (minimal diff)

```python
# Before:
# from datasets import load_dataset
# dataset = load_dataset("your/repo", name="mirror-merged", streaming=True)

# After:
from surrogate.data.cdn_datamodule import CDNDataModule

# Mac: run once after rate-limit window clears
# python scripts/build_dataset_manifest.py --date-folder 2026-05-03 --out manifest-2026-05-03.json

dm = CDNDataModule(
    manifest_path="manifest-2026-05-03.json",
    batch_size=8,
    num_workers=2,
)
train_loader = dm.train_dataloader()
```

---

### 5) Studio reuse guard (avoid quota burn)

`surrogate/lightning_utils.py`

```python
from lightning import Studio, Machine, Teamspace

def get_or_create_studio(name: str, machine: Machine = Machine.L40S):
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )
```

---

### 6) Runbook (one-time)

```bash
# 1) On Mac (after HF API window clears)
cd /opt/axentx/airship
python scripts/build_dataset_manifest.py \
  --date-folder 2026-05-03 \
  --out manifest-2026-05-03.json

# 2) Commit manifest to repo (small, deterministic)
git add manifest-2026-05-03.json
git commit -m "manifest: 2026-05
