# surrogate-1 / discovery

## Final Synthesized Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-only Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Core Architecture (Unified)

1. **`train_manifest.json` generation** (single source of truth)
   - Run once per day from Mac/CI via `list_repo_tree(recursive=False)` on today’s date folder.
   - Save `{"date": "YYYY-MM-DD", "files": ["path1.parquet", ...]}` to repo root.
   - Commit to repo so training never calls HF API for file listing.

2. **CDN-only dataset loader** (`src/data_cdn.py`)
   - Reads `train_manifest.json`.
   - Downloads each file via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth).
   - Projects to `{prompt, response}` only; drops extra columns.
   - Uses deterministic `slug-hash % 16` for shard assignment (mirrors runner logic).

3. **Lightning training stub** (`src/train.py`)
   - Uses `LightningDataModule` built on `data_cdn.py`.
   - Reuses running Studio if present; else starts L40S in `lightning-public-prod`.
   - Checks studio status before each run and restarts if stopped (idle-timeout resilience).

4. **GitHub Actions** (optional but recommended)
   - Add lightweight “manifest job” that runs once per day (or on cron) from a Mac/CI runner with HF token, writes `train_manifest.json` back to repo.
   - Keeps matrix ingest for raw public ingestion unchanged (still useful for fresh data), but training now depends only on the manifest + CDN.

---

### Final Code Snippets

#### 1. `scripts/gen_manifest.py` (run from Mac/CI)
```python
#!/usr/bin/env python3
"""
Generate train_manifest.json for a given date folder.
Run once per day (or per cron) on a Mac/CI with HF_TOKEN.
"""
import os, json, datetime
from huggingface_hub import HfApi

API = HfApi()
REPO = "axentx/surrogate-1-training-pairs"
DATE = datetime.date.today().isoformat()  # e.g. 2026-05-03
MANIFEST = "train_manifest.json"

def main() -> None:
    entries = API.list_repo_tree(
        repo_id=REPO,
        path=f"batches/public-merged/{DATE}",
        recursive=False,
    )
    files = [e.path for e in entries if e.path.endswith(".parquet")]
    manifest = {"date": DATE, "files": sorted(files)}
    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {MANIFEST} with {len(files)} files for {DATE}")

if __name__ == "__main__":
    main()
```

#### 2. `src/data_cdn.py`
```python
from __future__ import annotations
import json, pyarrow.parquet as pq, pyarrow.compute as pc
import numpy as np, requests, io, os
from typing import List, Dict, Any

REPO = "axentx/surrogate-1-training-pairs"
BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main"

def load_manifest(path: str = "train_manifest.json") -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)

def cdn_download(file_path: str) -> bytes:
    url = f"{BASE}/{file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content

def project_to_pair(buf: bytes) -> List[Dict[str, str]]:
    tbl = pq.read_table(io.BytesIO(buf), columns=["prompt", "response"])
    # Drop rows where prompt/response are null or empty
    mask = pc.and_(
        pc.is_valid(tbl["prompt"]),
        pc.is_valid(tbl["response"]),
        pc.utf8_length(tbl["prompt"]) > 0,
        pc.utf8_length(tbl["response"]) > 0,
    )
    tbl = tbl.filter(mask)
    return [
        {"prompt": str(p), "response": str(r)}
        for p, r in zip(tbl["prompt"].to_pylist(), tbl["response"].to_pylist())
    ]

def slug_hash(slug: str) -> int:
    # Deterministic, stable across runs
    import hashlib
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

class CDNDataModule:
    def __init__(self, manifest_path: str = "train_manifest.json", shard_id: int | None = None):
        self.manifest = load_manifest(manifest_path)
        self.shard_id = shard_id  # None = all files

    def load_dataset(self) -> List[Dict[str, str]]:
        pairs = []
        for fpath in self.manifest["files"]:
            # Optional shard filtering (mirrors runner bucket logic)
            if self.shard_id is not None:
                slug = os.path.splitext(os.path.basename(fpath))[0]
                if slug_hash(slug) % 16 != self.shard_id:
                    continue
            buf = cdn_download(fpath)
            pairs.extend(project_to_pair(buf))
        return pairs
```

#### 3. `src/train.py` (Lightning stub)
```python
#!/usr/bin/env python3
import lightning as L
from torch.utils.data import Dataset, DataLoader
from data_cdn import CDNDataModule

class PairDataset(Dataset):
    def __init__(self, pairs):
        self.pairs = pairs
    def __len__(self):
        return len(self.pairs)
    def __getitem__(self, idx):
        return self.pairs[idx]

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, shard_id=None, batch_size=8):
        super().__init__()
        self.dm = CDNDataModule(shard_id=shard_id)
        self.batch_size = batch_size
    def setup(self, stage=None):
        pairs = self.dm.load_dataset()
        self.dataset = PairDataset(pairs)
    def train_dataloader(self):
        return DataLoader(self.dataset, batch_size=self.batch_size, shuffle=True)

class SurrogateTrainer(L.LightningModule):
    def __init__(self):
        super().__init__()
        # TODO: model definition
        self.example_input = None
    def training_step(self, batch, batch_idx):
        # TODO: real training step
        self.log("loss", 0.0)
        return 0.0
    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

if __name__ == "__main__":
    import torch
    # Reuse running studio if available
    from lightning.pytorch import Studio
    studio_name = "surrogate-1-train"
    running = None
    for s in L.Teamspace().studios:
        if s.name == studio_name and s.status == "running":
            running = s
            break
    if running:
        print(f"Reusing running studio: {studio_name}")
        studio = running
    else:
        print(f"Starting new studio: {studio_name}")
        studio = L.Studio(
            name=studio_name,
            create_ok=True,
            machine=L.Machine.L40S,
            cloud=L.Cloud.LIGHTNING_PUBLIC_PROD,
        )
    # Ensure studio is running before launch
    if studio.status != "running":
        studio.start(machine=L.Machine.L40S)
    dm = SurrogateDataModule(shard_id=None)
    model = SurrogateTrainer()
    trainer = L.Trainer(max_epochs=1, accelerator="gpu", devices=1)
    trainer.fit(model, dm)
```

#### 4. GitHub Actions addition (optional)
Add to `.github/workflows/manifest.yml`:
```yaml
name: Generate training manifest
on:
  schedule:
    - cron: "0 2 * * *"   # daily 02:00 UTC
  workflow_dispatch:
jobs:
  manifest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python
