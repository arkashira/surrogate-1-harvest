# vanguard / discovery

## 1. Diagnosis
- No durable ingestion manifest: every training run re-lists HF repos via API → guaranteed 429s and quota burn.
- Training uses `load_dataset`/`list_repo_files` instead of CDN bypass → data loading hits auth-required API limits.
- No reuse guard for Lightning Studio → idle-stop + recreation burns 80h/mo quota.
- Missing pre-fetch file list artifact → training cannot run CDN-only.
- No clear path to run ingestion once and train many times without re-discovery.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/` with:
- `ingest_manifest.py` — one-time HF folder listing → JSON manifest (date-scoped)
- `train_cdn.py` — Lightning training that loads only from CDN using manifest (zero API calls during data load)
- `reuse_studio.py` — guard to find+reuse running studio or start new (L40S priority, fallback to public)
- `run_discovery.sh` — orchestration wrapper (shebang, executable) tying the three steps together

## 3. Implementation

```bash
# Create discovery module
mkdir -p /opt/axentx/vanguard/discovery
cd /opt/axentx/vanguard/discovery
```

### `ingest_manifest.py`
```python
#!/usr/bin/env python3
"""
One-time HF folder listing -> durable manifest for CDN-only training.
Run from Mac (or any HF-authed env) after rate-limit window clears.
"""
import json, os, sys, hashlib, datetime
from huggingface_hub import list_repo_tree

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx-mirror")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", datetime.date.today().isoformat())  # e.g. 2026-05-02
OUT_PATH = os.getenv("MANIFEST_OUT", "manifest.json")

def build_manifest(repo: str, folder: str, out: str):
    entries = []
    try:
        tree = list_repo_tree(repo, recursive=False, folder=folder)
        for node in tree:
            if node.type == "file":
                entries.append({
                    "repo": repo,
                    "path": f"{folder}/{node.path}",
                    "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{folder}/{node.path}"
                })
    except Exception as e:
        print(f"HF list error: {e}", file=sys.stderr)
        sys.exit(1)

    manifest = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "repo": repo,
        "folder": folder,
        "count": len(entries),
        "files": entries
    }

    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(entries)} entries -> {out}")

if __name__ == "__main__":
    build_manifest(HF_REPO, DATE_FOLDER, OUT_PATH)
```

### `train_cdn.py`
```python
#!/usr/bin/env python3
"""
Lightning training that uses CDN-only fetches via manifest.
Zero HF API calls during data loading.
"""
import json, os, sys, io, warnings
import lightning as L
import torch
from torch.utils.data import Dataset, DataLoader
import pyarrow.parquet as pq
import requests
from typing import List, Dict

warnings.filterwarnings("ignore")

MANIFEST = os.getenv("MANIFEST_PATH", "manifest.json")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "100"))

class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.urls = [f["cdn_url"] for f in self.manifest["files"] if f["path"].endswith(".parquet")]
        self.cache = {}

    def __len__(self) -> int:
        return max(1, len(self.urls) * 100)  # synthetic epochs over files

    def _load_parquet(self, url: str):
        if url in self.cache:
            return self.cache[url]
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        table = pq.read_table(io.BytesIO(resp.content))
        # Project to {prompt, response} only
        df = table.select(["prompt", "response"]).to_pandas()
        self.cache[url] = df
        return df

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        url = self.urls[idx % len(self.urls)]
        df = self._load_parquet(url)
        row = df.iloc[idx % len(df)]
        # Tokenize in-place or use tokenizer later; here we return raw strings as placeholder
        return {
            "prompt": str(row["prompt"]),
            "response": str(row["response"])
        }

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: str, batch_size: int = 4):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size

    def setup(self, stage=None):
        self.ds = CDNParquetDataset(self.manifest_path)

    def train_dataloader(self):
        return DataLoader(self.ds, batch_size=self.batch_size, shuffle=True, num_workers=0)

class SurrogateModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.net = torch.nn.Linear(1024, 1024)  # placeholder
        self.loss = torch.nn.MSELoss()

    def training_step(self, batch, batch_idx):
        # Replace with real tokenized tensors; placeholder loss
        x = torch.randn(1024)
        y = torch.randn(1024)
        pred = self.net(x)
        loss = self.loss(pred, y)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

if __name__ == "__main__":
    L.seed_everything(42)
    dm = SurrogateDataModule(MANIFEST, BATCH_SIZE)
    model = SurrogateModel()

    # Reuse guard handled externally; here we create trainer for local or studio run
    trainer = L.Trainer(
        max_steps=MAX_STEPS,
        accelerator="gpu" if torch.cuda.is_available() else "cpu",
        devices=1,
        log_every_n_steps=10,
        enable_checkpointing=False
    )
    trainer.fit(model, dm)
```

### `reuse_studio.py`
```python
#!/usr/bin/env python3
"""
Find+reuse running Lightning Studio or start new (L40S priority).
"""
import os, sys, time
import lightning as L

STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-l40s")
MACHINE = os.getenv("LIGHTNING_MACHINE", "L40S")  # Lightning will fallback to public if not in account

def get_running_studio(name: str):
    try:
        studios = L.Teamspace().studios
        for s in studios:
            if s.name == name and s.status == "running":
                return s
    except Exception:
        pass
    return None

def ensure_studio():
    studio = get_running_studio(STUDIO_NAME)
    if studio:
        print(f"Reusing running studio: {studio.name}")
        return studio

    print(f"No running studio '{STUDIO_NAME}'; starting new (machine={MACHINE})")
    # start new; Lightning will pick available cloud account
    studio = L.Studio(
        name=STUDIO_NAME,
        machine=MACHINE,
        create_ok=True
    )
    return studio

if __name__ == "__main__":
    studio = ensure_studio()
    print(f"Studio: {studio.name} status={studio.status}")
```

### `run_discovery.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
# Orchestration wrapper for vanguard discovery pipeline.
# Run from Mac (or any HF-authed env).

cd "$(dirname "$0
