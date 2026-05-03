# airship / discovery

## Incremental Improvement: Manifest-Driven CDN-Only Dataset Loader (Discovery Focus)

**Highest-value 2h ship**: Replace the current `load_dataset`/`list_repo_files` ingestion path with a manifest-driven, CDN-only loader that eliminates HF API 429s and `pyarrow.CastError` from heterogeneous repos.

---

## Implementation Plan

### 1. Create manifest generator (Mac orchestration)
Single API call to list one date folder → JSON manifest with CDN URLs.

```bash
# /opt/axentx/airship/surrogate/scripts/generate_manifest.py
#!/usr/bin/env python3
"""
Usage: python generate_manifest.py <repo> <date_folder> <out.json>
Example: python generate_manifest.py datasets/surrogate-mirror 2026-05-03 manifest.json
"""
import json, sys, os
from huggingface_hub import list_repo_tree

def main():
    repo = sys.argv[1]          # e.g. datasets/surrogate-mirror
    folder = sys.argv[2]        # e.g. 2026-05-03
    out_path = sys.argv[3]      # e.g. manifest.json

    # Single non-recursive call (paginates 100x if recursive)
    tree = list_repo_tree(repo_path=folder, repo_id=repo, recursive=False)
    files = [f for f in tree if f.type == "file" and f.path.endswith(".parquet")]

    manifest = {
        "repo": repo,
        "folder": folder,
        "files": [
            {
                "path": f.path,
                "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}"
            }
            for f in files
        ]
    }

    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/airship/surrogate/scripts/generate_manifest.py
```

---

### 2. CDN-only dataset loader (Lightning training)
Zero API calls during training; uses CDN URLs from embedded manifest.

```python
# /opt/axentx/airship/surrogate/train/cdn_dataset.py
import pyarrow.parquet as pq
import pyarrow.compute as pc
import requests
import io
import json
from pathlib import Path
from typing import List, Dict

class CDNParquetDataset:
    """
    Loads {prompt, response} pairs from parquet files via CDN URLs
    without HF API calls. Projects only required columns to avoid
    mixed-schema CastError.
    """
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.urls = [f["cdn_url"] for f in self.manifest["files"]]

    def _download_parquet(self, url: str):
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return pq.read_table(io.BytesIO(r.content))

    def __len__(self):
        return len(self.urls)

    def __getitem__(self, idx: int) -> Dict[str, str]:
        tbl = self._download_parquet(self.urls[idx])
        # Project only required fields; ignore extra columns
        prompt = pc.fill_null(tbl["prompt"], "").to_pylist()
        response = pc.fill_null(tbl["response"], "").to_pylist()
        # Flatten rows into individual samples
        return [
            {"prompt": p, "response": r}
            for p, r in zip(prompt, response)
            if p and r
        ]

    def items(self):
        for idx in range(len(self)):
            yield from self[idx]
```

---

### 3. Lightning training script integration
Reuse running Studio; embed manifest path; CDN-only data loading.

```python
# /opt/axentx/airship/surrogate/train/train_cdn.py
import lightning as L
from lightning.fabric.plugins import TorchMetrics
from surrogate.train.cdn_dataset import CDNParquetDataset
from torch.utils.data import DataLoader, IterableDataset
import json

class StreamingCDNDataset(IterableDataset):
    def __init__(self, manifest_path):
        self.manifest_path = manifest_path

    def __iter__(self):
        ds = CDNParquetDataset(self.manifest_path)
        yield from ds.items()

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path, batch_size=8):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size

    def train_dataloader(self):
        return DataLoader(
            StreamingCDNDataset(self.manifest_path),
            batch_size=self.batch_size,
            num_workers=0
        )

class SurrogateTrainer(L.LightningModule):
    def __init__(self):
        super().__init__()
        # Minimal model stub; replace with actual surrogate model
        self.model = None

    def training_step(self, batch, batch_idx):
        # batch: list of {"prompt":..., "response":...}
        loss = self.model.forward(batch) if self.model else sum(len(b["prompt"]) for b in batch) * 0.0
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        import torch
        return torch.optim.Adam(self.parameters(), lr=1e-4)

def main():
    manifest_path = "/opt/axentx/airship/surrogate/manifests/latest.json"
    dm = SurrogateDataModule(manifest_path)
    model = SurrogateTrainer()

    # Reuse running studio if available
    from lightning.fabric.plugins.environments import LightningEnvironment
    trainer = L.Trainer(
        max_epochs=1,
        accelerator="gpu",
        devices=1,
        environment=LightningEnvironment(),
        limit_train_batches=10
    )
    trainer.fit(model, dm)

if __name__ == "__main__":
    main()
```

---

### 4. Cron-safe wrapper (prevent script errors)
Ensure Bash shebang and executable bit; set SHELL in crontab.

```bash
# /opt/axentx/airship/surrogate/scripts/run_cdn_ingest.sh
#!/usr/bin/env bash
set -euo pipefail

cd /opt/axentx/airship/surrogate

# Generate manifest (single API call)
python scripts/generate_manifest.py datasets/surrogate-mirror 2026-05-03 manifests/latest.json

# Launch Lightning training (reuses studio; CDN-only)
python train/train_cdn.py
```

```bash
chmod +x /opt/axentx/airship/surrogate/scripts/run_cdn_ingest.sh
```

Crontab entry (set SHELL to avoid cron issues):

```cron
SHELL=/bin/bash
0 2 * * * /opt/axentx/airship/surrogate/scripts/run_cdn_ingest.sh >> /var/log/airship_cdn_ingest.log 2>&1
```

---

## Verification Steps (2h checklist)

- [ ] `python scripts/generate_manifest.py` produces valid JSON with CDN URLs.
- [ ] `CDNParquetDataset` downloads and projects `{prompt, response}` without `pyarrow.CastError`.
- [ ] Lightning training runs with zero HF API calls during data load (check logs for no 429s).
- [ ] Running Studio is reused (list studios before `Trainer`).
- [ ] Wrapper script has shebang, is executable, and cron uses `SHELL=/bin/bash`.
