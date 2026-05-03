# vanguard / backend

## Final Synthesized Implementation (Best of Both Candidates)

### 1. Diagnosis (Consensus)
- **CDN manifest missing**: ingestion/training can still trigger `list_repo_tree`/`load_dataset` at runtime → 429 risk and non-reproducible runs.
- **No deterministic, content-addressed file list**: surrogate training cannot pin exact files without re-querying HF API.
- **No Mac-first orchestration**: violates “Mac=CLI rule + heavy compute on remote” and risks quota burn from repeated Lightning Studio creation.
- **No idle-stop resilience**: Lightning Studio stops idle → subsequent `.run()` fails instead of restarting.

### 2. Architecture (Single Source of Truth)
- **Mac/CLI-only manifest generation**: one `list_repo_tree` call per date folder → deterministic `cdn_manifest_{date}.json` with content-addressed entries.
- **CDN-only training**: training script consumes manifest and fetches exclusively via CDN URLs (zero HF API calls during training).
- **Resilient orchestrator**: reuse running Lightning Studio (L40S), auto-restart on idle-stop, forward manifest path, and enforce single active studio.

### 3. Implementation

```bash
mkdir -p /opt/axentx/vanguard/backend
```

#### `/opt/axentx/vanguard/backend/generate_cdn_manifest.py`
```python
#!/usr/bin/env python3
"""
generate_cdn_manifest.py
Mac/CLI-only: produce CDN manifest for one date folder.
Run: python generate_cdn_manifest.py <repo> <date_folder> [out_dir]
"""
import json, hashlib, sys, time
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

def build_manifest(repo: str, date_folder: str, out_dir: str = ".") -> Path:
    api = HfApi()
    # Non-recursive list for chosen date folder only (rate-limit friendly)
    files = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    entries: List[Dict[str, object]] = []
    for f in files:
        if not f.path.endswith((".parquet", ".jsonl", ".json")):
            continue
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}"
        # Content-addressed by path+size (cheap; avoids extra download)
        digest = hashlib.sha256(f"{f.path}::{f.size}".encode()).hexdigest()[:16]
        entries.append({
            "repo": repo,
            "path": f.path,
            "size": f.size,
            "sha256_prefix": digest,
            "cdn_url": cdn_url,
        })

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": repo,
        "date_folder": date_folder,
        "total_files": len(entries),
        "entries": entries,
    }

    out_path = Path(out_dir) / f"cdn_manifest_{date_folder.replace('/', '_')}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(entries)} entries to {out_path}")
    return out_path

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python generate_cdn_manifest.py <repo> <date_folder> [out_dir]")
        sys.exit(1)
    build_manifest(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ".")
```

#### `/opt/axentx/vanguard/backend/train_surrogate.py`
```python
#!/usr/bin/env python3
"""
train_surrogate.py
Lightning training script that uses CDN-only fetches.
Expects --manifest <path> and optional --output_dir.
"""
import json, argparse, os
from pathlib import Path
from typing import List, Dict

import torch
from torch.utils.data import Dataset, DataLoader
import requests

try:
    import lightning as L
except ImportError:
    print("Install: pip install lightning")
    raise

class CDNParquetDataset(Dataset):
    def __init__(self, entries: List[Dict], max_files: int = None):
        self.entries = entries[:max_files] if max_files else entries

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, idx):
        e = self.entries[idx]
        resp = requests.get(e["cdn_url"], timeout=30)
        resp.raise_for_status()
        # Placeholder: return bytes length as dummy feature
        return {"size": len(resp.content), "cdn_url": e["cdn_url"]}

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: str, batch_size: int = 8, max_files: int = None):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size
        self.max_files = max_files
        self.entries = json.loads(Path(manifest_path).read_text())["entries"]

    def setup(self, stage=None):
        self.ds = CDNParquetDataset(self.entries, max_files=self.max_files)

    def train_dataloader(self):
        return DataLoader(self.ds, batch_size=self.batch_size, shuffle=True, num_workers=0)

class SurrogateModel(L.LightningModule):
    def __init__(self, lr: float = 1e-3):
        super().__init__()
        self.net = torch.nn.Linear(1, 1)
        self.lr = lr

    def training_step(self, batch, batch_idx):
        x = torch.tensor([b["size"] for b in batch], dtype=torch.float32).unsqueeze(1)
        y = self.net(x)
        loss = y.mean()  # dummy
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.net.parameters(), lr=self.lr)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to cdn_manifest_*.json")
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--batch_size", type=int, default=4)
    parser.add_argument("--max_files", type=int, default=None)
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    dm = SurrogateDataModule(
        manifest_path=args.manifest,
        batch_size=args.batch_size,
        max_files=args.max_files,
    )
    model = SurrogateModel()

    trainer = L.Trainer(
        max_epochs=args.epochs,
        default_root_dir=args.output_dir,
        accelerator="auto",
        devices=1,
        enable_checkpointing=False,
        logger=False,
    )
    trainer.fit(model, dm)
    print("Training complete (CDN-only).")

if __name__ == "__main__":
    main()
```

#### `/opt/axentx/vanguard/backend/run_training.py`
```python
#!/usr/bin/env python3
"""
run_training.py
Mac/CLI orchestrator: reuse or start Lightning Studio, handle idle-stop restart.
"""
import sys, time, subprocess, json
from pathlib import Path

try:
    import lightning as L
    from lightning.app import LightningFlow, LightningApp
    from lightning.app.components import LightningTrainer
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

MANIFEST_DIR = Path(__file__).parent
DEFAULT_MANIFEST = MANIFEST_DIR / "cdn_manifest_latest.json"

class TrainingFlow(LightningFlow):
    def __init__(self, manifest_path: str, training_script: str, script_args: list):
        super().__init__()
        self.manifest_path = manifest_path
        self.training_script = training_script
        self.script_args = script_args
        self._trainer_component = None
        self._studio_started = False

    def configure_layout(self):
        return []

    def run(self):
        if self._trainer_component is None or not self._trainer_component.is_running:
            # Build trainer component pointing
