# vanguard / backend

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest → repeated authenticated `list_repo_tree` calls burn HF API quota and trigger 429s.
- Training script likely uses `load_dataset(streaming=True)` on heterogeneous repos → `pyarrow.CastError` on mixed schemas.
- No CDN-only data path → every shard fetch goes through `/api/` auth layer and counts against rate limits.
- No Lightning Studio reuse guard → each run recreates studio and burns 80+ quota hours/month.
- No idle-stop resilience → Lightning idle timeout kills long training jobs without restart.

## 2. Proposed change
Add a lightweight manifest-based ingestion wrapper and a resilient Lightning launcher:
- Create `/opt/axentx/vanguard/backend/manifest.py` — non-recursive, date-folder-scoped JSON manifest generator + CDN URL builder.
- Create `/opt/axentx/vanguard/backend/train.py` — surrogate-1 training entrypoint that:
  - loads a pre-generated manifest (CDN-only URLs),
  - streams parquet shards via `datasets` without `streaming=True` on heterogeneous repos,
  - reuses a running Lightning Studio or restarts if idle-stopped.
- Touch `/opt/axentx/vanguard/backend/requirements.txt` to include `lightning-ai`, `datasets`, `pyarrow`, `requests`.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/requirements.txt
lightning-ai>=0.18.0
datasets>=2.18.0
pyarrow>=14.0.0
requests>=2.31.0
tqdm>=4.66.0
```

```python
# /opt/axentx/vanguard/backend/manifest.py
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List
import requests

HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"

def list_date_folder(repo: str, date_folder: str, token: str = None) -> List[str]:
    """
    Non-recursive listing for a single date folder.
    Returns filenames relative to date_folder.
    """
    url = f"{HF_API_BASE}/repos/datasets/{repo}/tree/{date_folder}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 429:
        raise RuntimeError("HF API rate-limited 429 — wait 360s")
    resp.raise_for_status()
    items = resp.json()
    # API returns list of dicts with 'path' and 'type'
    files = [item["path"] for item in items if item.get("type") == "file"]
    return files

def build_manifest(repo: str, date_folder: str, out_dir: str, token: str = None) -> Path:
    """
    Persist manifest:
    {
      "repo": "...",
      "date_folder": "...",
      "created_at": "...",
      "files": [...],
      "cdn_urls": [...]
    }
    """
    files = list_date_folder(repo, date_folder, token=token)
    cdn_urls = [
        f"{HF_CDN_BASE}/{repo}/resolve/main/{f}"
        for f in files
    ]
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "cdn_urls": cdn_urls,
    }
    out_path = Path(out_dir) / f"manifest_{repo.replace('/', '_')}_{date_folder}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path

def load_manifest(manifest_path: str) -> Dict:
    return json.loads(Path(manifest_path).read_text())
```

```python
# /opt/axentx/vanguard/backend/train.py
import os
import json
from pathlib import Path
from typing import Dict, Iterator

import torch
from datasets import IterableDataset, load_dataset
from lightning.pytorch import Trainer
from lightning.pytorch.strategies import DeepSpeedStrategy
from lightning.pytorch.loggers import CSVLogger

from .manifest import load_manifest

HF_TOKEN = os.getenv("HF_TOKEN")

class CDNParquetDataset(IterableDataset):
    """
    CDN-only parquet shard streamer.
    Assumes each parquet has at least {prompt,response} fields.
    """
    def __init__(self, manifest_path: str, split: str = "train"):
        manifest = load_manifest(manifest_path)
        urls = manifest["cdn_urls"]
        # Filter to parquet only
        parquet_urls = [u for u in urls if u.endswith(".parquet")]
        if not parquet_urls:
            raise ValueError("No parquet files in manifest")
        # Use datasets with split via data_files (avoids load_dataset(streaming=True) on mixed repos)
        self.dataset = load_dataset(
            "parquet",
            name=split,
            data_files={"train": parquet_urls},
            split=split,
            streaming=True,
            token=HF_TOKEN,
        )

    def __iter__(self) -> Iterator[Dict]:
        for item in self.dataset:
            # Project to minimal schema expected by surrogate-1
            yield {
                "prompt": item.get("prompt") or item.get("instruction") or "",
                "response": item.get("response") or item.get("output") or "",
            }

class Surrogate1Model(torch.nn.Module):
    """Minimal LM head for surrogate training (replace with real model)."""
    def __init__(self, vocab_size: int = 50257, d_model: int = 1024):
        super().__init__()
        self.embed = torch.nn.Embedding(vocab_size, d_model)
        self.lm_head = torch.nn.Linear(d_model, vocab_size)

    def forward(self, input_ids):
        x = self.embed(input_ids)
        return self.lm_head(x.mean(dim=1))

class Surrogate1LitModule(torch.nn.Module):
    """LightningModule wrapper (simplified)."""
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.loss_fn = torch.nn.CrossEntropyLoss()

    def training_step(self, batch, batch_idx):
        prompt = batch["prompt"]
        # tokenize externally or mock for now
        input_ids = torch.randint(0, 50257, (len(prompt), 64), device=self.device)
        labels = torch.randint(0, 50257, (len(prompt),), device=self.device)
        logits = self.model(input_ids)
        loss = self.loss_fn(logits, labels)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=1e-4)

def get_or_create_studio(name: str = "vanguard-surrogate-train"):
    from lightning.pytorch.cloud import Studio, Teamspace, Machine
    # Reuse running studio to save quota
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    # Create new if none running
    return Studio(
        name=name,
        machine=Machine.L40S,
        create_ok=True,
    )

def run_training(manifest_path: str, max_steps: int = 1000):
    studio = get_or_create_studio()
    # If stopped, restart
    if studio.status != "Running":
        studio.start(machine="L40S")

    dataset = CDNParquetDataset(manifest_path)
    model = Surrogate1LitModule(Surrogate1Model())

    trainer = Trainer(
        max_steps=max_steps,
        logger=CSVLogger(save_dir="logs"),
        strategy=DeepSpeedStrategy("zero_stage_2"),
        devices=1,
    )
    trainer.fit(model, train_dataloaders=dataset)

if __name__ == "__main__":
    # Example usage (run from orchestration on Mac):
    # HF_TOKEN=... python -m vanguard.backend.train /path/to/manifest.json
    import sys
    manifest_p = sys.argv[1] if len(sys.argv) > 1 else "
