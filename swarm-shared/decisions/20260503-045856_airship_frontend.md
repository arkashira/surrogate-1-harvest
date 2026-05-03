# airship / frontend

## Highest-Value Incremental Improvement (≤2h)

**Embed a CDN-only file manifest into Surrogate-1 training** so Lightning Studio training runs with **zero HF API calls during data loading**, eliminating 429 rate-limit failures and quota burn during iteration.

---

## Implementation Plan

### 1. Generate manifest on Mac (or any dev box) — one-time per date folder
- Use `list_repo_tree(path, recursive=False)` for a single date folder under the surrogate dataset repo.
- Save to `training/manifests/{date}_manifest.json` containing only `{ "files": ["path1.parquet", ...], "repo": "org/surrogate-dataset", "date": "2026-04-29" }`.
- Commit manifest alongside training code (small, versioned).

### 2. Update `train.py` to use CDN-only fetches
- Read manifest at launch.
- Build URLs: `f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"`.
- Use `IterableDataset` that yields URLs (or downloads via `aiohttp`/`requests` with streaming).
- Parse only `{prompt, response}` from each Parquet file in-memory.
- Zero `load_dataset`, zero `list_repo_files`, zero HF API auth during training.

### 3. Lightning Studio integration
- Reuse running studio if available (`Teamspace.studios` check).
- Pass manifest path as script arg or embed directly in repo.
- On idle-stop, restart with `target.start(machine=Machine.L40S)` before next `.run()`.

### 4. Fallback & safety
- If CDN fetch fails (rare), log and skip file; continue training.
- Validate schema on first file; warn if missing `prompt`/`response`.

---

## Code Snippets

### Manifest generator (run on dev box)
```python
# scripts/gen_cdn_manifest.py
import json
import os
from huggingface_hub import HfApi

def gen_manifest(repo: str, date_folder: str, out_path: str):
    api = HfApi()
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [item.path for item in tree if item.path.endswith(".parquet")]
    manifest = {
        "repo": repo,
        "date": date_folder,
        "files": sorted(files),
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest saved to {out_path} ({len(files)} files)")

if __name__ == "__main__":
    # Example: python gen_cdn_manifest.py org/surrogate-dataset 2026-04-29 ./training/manifests/2026-04-29_manifest.json
    import sys
    _, repo, date_folder, out_path = sys.argv
    gen_manifest(repo, date_folder, out_path)
```

### CDN-only IterableDataset
```python
# training/dataset.py
import pyarrow.parquet as pq
import requests
import io
import torch
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path, repo, max_retries=3):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.repo = repo or self.manifest["repo"]
        self.files = self.manifest["files"]
        self.max_retries = max_retries

    def _fetch_parquet(self, path):
        url = f"https://huggingface.co/datasets/{self.repo}/resolve/main/{path}"
        for attempt in range(self.max_retries):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                return pq.read_table(io.BytesIO(resp.content))
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise
        return None

    def __iter__(self):
        for path in self.files:
            try:
                table = self._fetch_parquet(path)
                if table is None:
                    continue
                df = table.select(["prompt", "response"]).to_pandas()
                for _, row in df.iterrows():
                    if pd.notna(row.get("prompt")) and pd.notna(row.get("response")):
                        yield {"prompt": row["prompt"], "response": row["response"]}
            except Exception as e:
                print(f"Skipping {path}: {e}")
                continue
```

### Lightning training script snippet
```python
# train.py
from lightning.pytorch import Trainer
from lightning.pytorch.demos.boring_classes import BoringModel
from training.dataset import CDNParquetDataset
from torch.utils.data import DataLoader
import json, os

class SurrogateTrainer(BoringModel):
    def __init__(self, manifest_path, repo):
        super().__init__()
        self.dataset = CDNParquetDataset(manifest_path, repo)

    def train_dataloader(self):
        return DataLoader(self.dataset, batch_size=8, num_workers=4)

if __name__ == "__main__":
    manifest_path = "training/manifests/2026-04-29_manifest.json"
    repo = "org/surrogate-dataset"
    model = SurrogateTrainer(manifest_path, repo)
    trainer = Trainer(max_epochs=1, accelerator="gpu", devices=1)
    trainer.fit(model)
```

### Studio reuse + safe start (orchestration snippet)
```python
# launch_studio.py
from lightning.pytorch.cli import LightningCLI
from lightning import Studio, Teamspace, Machine, L40S

def get_or_start_studio(name="surrogate-training"):
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Starting new studio: {name}")
    return Studio(
        name=name,
        machine=Machine.L40S,
        target="train.py",
        create_ok=True,
    )

if __name__ == "__main__":
    studio = get_or_start_studio()
    # If stopped, restart before run
    if studio.status != "Running":
        studio.start(machine=Machine.L40S)
    studio.run()
```

---

## Expected Outcome
- Training iterations no longer blocked by HF API 429s.
- Lightning quota preserved (no repeated dataset reloads).
- Faster iteration: manifest + CDN fetches only.
