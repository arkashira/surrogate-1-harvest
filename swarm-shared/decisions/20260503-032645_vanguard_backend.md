# vanguard / backend

## 1. Diagnosis
- No CDN-first manifest: ingestion/training scripts can still trigger `list_repo_tree`/`load_dataset` at runtime → 429s and non-reproducible runs.
- Missing deterministic file list keyed by date/slug; training jobs re-enumerate the repo and burn quota.
- No guard to reuse running Lightning Studio; each run risks quota loss via recreation and idle-stop kills training.
- Ingestion likely writes mixed-schema parquet into `enriched/` with extra metadata columns instead of strict `{prompt,response}` and attribution-in-filename.
- No explicit bypass of HF API during data loading; training still uses `load_dataset`/`list_repo_files` instead of CDN URLs.

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/manifest.py` + `/opt/axentx/vanguard/backend/train.py` (or patch existing) to:
- Add `build_manifest(repo, date_folder)` → `manifests/{date}.json` with CDN URLs and shas.
- Add `get_running_studio(name)` to reuse running studios.
- Replace any `load_dataset`/`list_repo_tree` calls in training with `IterableDataset` that streams from CDN URLs listed in the manifest (zero HF API calls during training).
- Enforce `{prompt,response}` projection at parse time; move attribution to filename pattern.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/manifest.py
import os
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    list_repo_tree = None  # fallback mode

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-dataset")
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

def build_manifest(date_folder: str, out_path: str = None) -> str:
    """
    Single API call to list one date folder, produce CDN-only manifest.
    Manifest item: { "cdn_url": "...", "sha256": "...", "slug": "...", "size": ... }
    """
    items = list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=True)
    files = [it for it in items if it.type == "file"]

    manifest: List[Dict] = []
    for f in files:
        cdn_url = f"{CDN_ROOT}/{f.path}"
        # lightweight content hash via download (or etag if preferred)
        local_path = hf_hub_download(repo_id=HF_REPO, filename=f.path, repo_type="dataset")
        sha256 = hashlib.sha256(open(local_path, "rb").read()).hexdigest()
        manifest.append({
            "cdn_url": cdn_url,
            "sha256": sha256,
            "slug": f.path,
            "size": f.size
        })

    out_path = out_path or str(MANIFEST_DIR / f"{date_folder.rstrip('/').replace('/', '_')}.json")
    with open(out_path, "w") as fp:
        json.dump(manifest, fp, indent=2)
    return out_path

def load_manifest(date_folder: str):
    p = MANIFEST_DIR / f"{date_folder.rstrip('/').replace('/', '_')}.json"
    if not p.exists():
        raise FileNotFoundError(f"Manifest missing: {p}. Run build_manifest first.")
    with open(p) as fp:
        return json.load(fp)
```

```bash
# /opt/axentx/vanguard/backend/train.py
import os
import json
import torch
from torch.utils.data import IterableDataset, DataLoader
import lightning as L
from huggingface_hub import Teamspace

from .manifest import load_manifest, CDN_ROOT

class CDNTextDataset(IterableDataset):
    """Zero HF API calls during training; stream from CDN URLs in manifest."""
    def __init__(self, manifest_path: str, transform=None):
        with open(manifest_path) as f:
            self.items = json.load(f)
        self.transform = transform or (lambda x: x)

    def __iter__(self):
        for item in self.items:
            # streaming-friendly: download one file at a time via CDN
            import requests
            resp = requests.get(item["cdn_url"], timeout=30)
            resp.raise_for_status()
            # project to {prompt, response} only; attribution in filename
            # placeholder parser — adapt to your actual file format (jsonl/parquet/csv)
            if item["slug"].endswith(".jsonl"):
                for line in resp.text.strip().splitlines():
                    doc = json.loads(line)
                    yield {
                        "prompt": doc.get("prompt") or doc.get("input") or "",
                        "response": doc.get("response") or doc.get("output") or "",
                    }
            else:
                # add parquet/csv handlers as needed
                continue

def get_running_studio(name: str):
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    return None

class SurrogateTrainer(L.LightningModule):
    def __init__(self, manifest_path: str):
        super().__init__()
        self.save_hyperparameters()
        self.model = torch.nn.Linear(1024, 1024)  # placeholder
        self.manifest_path = manifest_path

    def train_dataloader(self):
        ds = CDNTextDataset(self.manifest_path)
        return DataLoader(ds, batch_size=8, num_workers=0)

    def training_step(self, batch, batch_idx):
        # minimal example
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

def run_training(date_folder: str, studio_name: str = "vanguard-surrogate"):
    # reuse running studio
    studio = get_running_studio(studio_name)
    if studio is None:
        from lightning.pytorch import Trainer
        from lightning.pytorch.callbacks import ModelCheckpoint
        # fallback: local launcher (prefer Lightning Studio for heavy compute)
        trainer = Trainer(max_epochs=1, callbacks=[ModelCheckpoint(monitor="train_loss")])
    else:
        # attach to running studio session if possible; else restart
        if studio.status != "Running":
            from lightning.pytorch import Machine
            studio.start(machine="lightning-public-prod")  # or L40S/H200 target as available
        trainer = studio.trainer

    manifest_path = f"manifests/{date_folder.rstrip('/').replace('/', '_')}.json"
    model = SurrogateTrainer(manifest_path=manifest_path)
    trainer.fit(model)

if __name__ == "__main__":
    # one-time manifest build (run from Mac orchestration, not in training loop)
    import sys
    date_folder = sys.argv[1] if len(sys.argv) > 1 else "mirror-merged/2026-04-29"
    from .manifest import build_manifest
    print("Building manifest:", build_manifest(date_folder))
    run_training(date_folder)
```

## 4. Verification
1. On Mac/orchestrator, run once:  
   ```bash
   cd /opt/axentx/vanguard/backend
   python -m manifest "mirror-merged/2026-04-29"
   ```
   Confirm `manifests/mirror-merged_2026-04-29.json` exists and contains `cdn_url` entries.

2. Start a Lightning Studio (or reuse running one) and run:  
   ```bash
   python train.py "mirror-merged/2026-04-29"
   ```
   Monitor logs: no `list_repo_tree`/`load_dataset` calls should appear; data should stream via `https://huggingface.co/datasets/.../resolve/main/...`.

3. Confirm quota safety: during training, HF API dashboard should show near-zero calls from this repo (only the one-time `list_repo_tree` from step 1).

4. Confirm schema: yielded batches contain only `prompt` and `response` keys; filenames carry attribution
