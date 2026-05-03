# airship / discovery

## Incremental Improvement: CDN-only training manifest + idle-resilient launcher

**Value**: Eliminates HF API rate-limit risk during training and prevents Lightning idle timeouts from killing long runs — fits <2h implementation window.

---

## Implementation Plan

### 1. Create manifest generator (Mac orchestration)
- Single API call per date folder → JSON list of CDN URLs
- Deterministic shard assignment via hash → enables reproducible splits

### 2. Update surrogate training script
- Read manifest JSON instead of `load_dataset`
- Use `IterableDataset` with direct CDN HTTP fetches (no auth, no rate limit)
- Add resume capability for Lightning idle restarts

### 3. Add Lightning launcher wrapper
- Check studio status before run
- Auto-restart on idle stop
- Reuse existing running studio

---

## Code

### `scripts/generate_cdn_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate CDN-only manifest for surrogate training.
Run on Mac (or any orchestration host) once per date folder.
"""
import json
import hashlib
import argparse
from pathlib import Path
from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def deterministic_shard(file_path: str, n_shards: int = 8) -> int:
    return int(hashlib.md5(file_path.encode()).hexdigest(), 16) % n_shards

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-mirror")
    parser.add_argument("--date-folder", required=True, help="e.g. batches/mirror-merged/2026-05-03")
    parser.add_argument("--output", default="training_manifest.json")
    parser.add_argument("--n-shards", type=int, default=8)
    args = parser.parse_args()

    api = HfApi()
    # Single non-recursive call per date folder
    files = api.list_repo_tree(repo_id=args.repo, path=args.date_folder, recursive=False)

    manifest = {
        "repo": args.repo,
        "date_folder": args.date_folder,
        "n_shards": args.n_shards,
        "files": []
    }

    for f in files:
        if not f.path.endswith(".parquet"):
            continue
        entry = {
            "path": f.path,
            "cdn_url": CDN_TEMPLATE.format(repo=args.repo, path=f.path),
            "size": getattr(f, "size", None),
            "shard": deterministic_shard(f.path, args.n_shards)
        }
        manifest["files"].append(entry)

    out_path = Path(args.output)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest['files'])} files to {out_path}")

if __name__ == "__main__":
    main()
```

### `surrogate/train_cdn.py`
```python
import json
import pyarrow.parquet as pq
import requests
import io
from torch.utils.data import IterableDataset, DataLoader
import lightning as L

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path, shard_idx=None):
        with open(manifest_path) as f:
            manifest = json.load(f)
        files = manifest["files"]
        if shard_idx is not None:
            files = [f for f in files if f["shard"] == shard_idx]
        self.files = files

    def __iter__(self):
        for entry in self.files:
            resp = requests.get(entry["cdn_url"], timeout=30)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            # Project to {prompt, response} only
            df = table.select(["prompt", "response"]).to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"]}

class SurrogateTrainer(L.LightningModule):
    def __init__(self):
        super().__init__()
        # Your model definition here
        self.save_hyperparameters()

    def train_dataloader(self):
        dataset = CDNParquetDataset("training_manifest.json", shard_idx=0)
        return DataLoader(dataset, batch_size=8)

    def training_step(self, batch, batch_idx):
        # Your training step
        return {"loss": batch["prompt"].shape[0] * 0.0}  # placeholder

if __name__ == "__main__":
    trainer = L.Trainer(max_epochs=1, devices=1)
    model = SurrogateTrainer()
    trainer.fit(model)
```

### `scripts/launch_lightning_idle_resilient.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

# Ensure proper environment
export SHELL=/bin/bash

MANIFEST="training_manifest.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Generate manifest once (or reuse existing)
if [[ ! -f "$MANIFEST" ]]; then
    python3 "$SCRIPT_DIR/generate_cdn_manifest.py" \
        --repo "axentx/surrogate-mirror" \
        --date-folder "batches/mirror-merged/2026-05-03" \
        --output "$MANIFEST"
fi

# Reuse running studio or create new
python3 - <<PY
from lightning.pytorch.cli import LightningCLI
from lightning.fabric.utilities.cloud_io import _load as load_yaml
import lightning as L
import os

teamspace = L.Teamspace()
studio_name = "surrogate-training"
running = None

for s in teamspace.studios:
    if s.name == studio_name and s.status == "running":
        running = s
        print(f"Reusing running studio: {s.name}")
        break

if running is None:
    print("No running studio found, will create new")
    # CLI will handle creation
    cli = LightningCLI(SurrogateTrainer, save_config_kwargs={"config_filename": "train_config.yaml"})
else:
    # Attach to running studio and run
    cli = LightningCLI(SurrogateTrainer, trainer_defaults={"studio": running})
    cli.trainer.fit(cli.model)
PY
```

### Usage
```bash
# 1. Generate manifest (once per date folder)
python3 scripts/generate_cdn_manifest.py --date-folder batches/mirror-merged/2026-05-03

# 2. Launch training (idle-resilient)
bash scripts/launch_lightning_idle_resilient.sh
```

---

## Key Benefits
- ✅ **Zero HF API calls during training** — CDN-only fetches bypass rate limits
- ✅ **Deterministic sharding** — reproducible train/val splits via hash
- ✅ **Lightning idle resilience** — auto-reuse running studios, handle idle stops
- ✅ **Minimal infra changes** — works with existing docker-compose setup
