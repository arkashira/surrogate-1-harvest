# airship / discovery

## Highest-Value Incremental Improvement (<2h)

**Goal**: Make Surrogate-1 training resilient to HF API 429s and Lightning Studio idle-stop by implementing deterministic CDN-only data loading + studio lifecycle reuse.

**Why this ships fast**:
- Single-file changes to data loader + training launcher
- No infra changes, no new dependencies
- Reuses existing patterns (CDN bypass, studio reuse)
- Immediate payoff: zero API calls during training, quota-safe studio reuse

---

## Implementation Plan

### 1. Create `tools/build_filelist.py` (Mac orchestration)
Run once (or per new date folder) after rate-limit window clears. Produces `filelist.json` for CDN-only training.

```python
#!/usr/bin/env python3
"""
Usage: python tools/build_filelist.py \
  --repo huggingface-datasets/airship-mirror \
  --date 2026-05-03 \
  --out surrogate/data/filelist.json
"""
import argparse
import json
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    # Single API call: non-recursive per folder to avoid pagination/429
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date,
        recursive=False,
        repo_type="dataset"
    )

    # Keep only parquet files; store relative paths for CDN fetch
    files = [
        f"{args.date}/{e.path.split('/')[-1]}"
        for e in entries
        if e.path.endswith(".parquet")
    ]

    filelist = {
        "repo": args.repo,
        "date": args.date,
        "files": sorted(files),
        "total": len(files)
    }

    import os
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(filelist, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x tools/build_filelist.py
```

---

### 2. Update `surrogate/data/dataset.py` — CDN-only loader

Replace any `load_dataset(streaming=True)` with CDN fetch using the pre-built filelist.

```python
import json
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset
from typing import Iterator

class CDNParquetDataset(IterableDataset):
    """
    CDN-only parquet loader.
    filelist.json format:
    {
      "repo": "huggingface-datasets/airship-mirror",
      "date": "2026-05-03",
      "files": ["2026-05-03/shard-001.parquet", ...],
      "total": 128
    }
    """
    def __init__(self, filelist_path: str, columns=("prompt", "response")):
        with open(filelist_path) as f:
            meta = json.load(f)
        self.repo = meta["repo"]
        self.files = meta["files"]
        self.columns = columns

    def _cdn_url(self, path: str) -> str:
        # CDN bypass: no Authorization header required
        return f"https://huggingface.co/datasets/{self.repo}/resolve/main/{path}"

    def _stream_shard(self, path: str) -> Iterator[dict]:
        url = self._cdn_url(path)
        # Stream download to temp file to avoid loading all shards in RAM
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    tmp.write(chunk)
                tmp.flush()
                table = pq.read_table(tmp.name, columns=self.columns)
                for i in range(table.num_rows):
                    row = {col: table[col][i].as_py() for col in self.columns}
                    yield row

    def __iter__(self) -> Iterator[dict]:
        for path in self.files:
            yield from self._stream_shard(path)
```

---

### 3. Update `surrogate/train.py` — Studio lifecycle + zero API during train

```python
import os
import json
from pathlib import Path
from lightning import Fabric, LightningFlow
from lightning.app import LightningApp
from lightning.app.utilities.cloud import _get_project
from lightning.app.storage import Drive

# Detect if running in Lightning Studio
IN_STUDIO = os.getenv("LIGHTNING_APP_ID") is not None

def get_or_create_studio():
    """
    Reuse running Studio if exists; avoid recreation to save quota.
    """
    try:
        from lightning.app import Teamspace
        for studio in Teamspace().studios:
            if studio.name == "surrogate-training" and studio.status == "Running":
                print(f"Reusing running studio: {studio.name}")
                return studio
    except Exception as e:
        print(f"Studio listing unavailable (local/dev): {e}")
    return None

def train():
    # Always use pre-built filelist (generated on Mac)
    filelist_path = Path("surrogate/data/filelist.json")
    if not filelist_path.exists():
        raise FileNotFoundError(
            "Run tools/build_filelist.py first to generate CDN filelist"
        )

    from surrogate.data.dataset import CDNParquetDataset
    from torch.utils.data import DataLoader

    dataset = CDNParquetDataset(filelist_path)
    loader = DataLoader(dataset, batch_size=8, num_workers=2)

    # Minimal training loop (replace with your Surrogate-1 trainer)
    fabric = Fabric(devices=1, accelerator="cuda", precision="bf16-mixed")
    fabric.launch()

    # Dummy model placeholder — swap with Surrogate-1 model
    import torch
    model = torch.nn.Transformer(d_model=512, nhead=8)
    model, optimizer = fabric.setup(model, torch.optim.AdamW(model.parameters(), lr=1e-4))

    model.train()
    for epoch in range(3):
        for batch in loader:
            # Project to tensors (simplified)
            prompts = batch["prompt"]  # handle tokenization upstream or here
            # Dummy step
            loss = torch.tensor(0.0, requires_grad=True)
            fabric.backward(loss)
            optimizer.step()
            optimizer.zero_grad()
        print(f"Epoch {epoch} done")

if __name__ == "__main__":
    # Studio reuse guard
    studio = get_or_create_studio()
    if IN_STUDIO and studio is None:
        print("No running studio found; starting fresh (may consume quota)")

    train()
```

---

### 4. Add launcher script `run_training.sh`

```bash
#!/usr/bin/env bash
# Ensures proper environment and avoids cron/bash issues
set -euo pipefail
export SHELL=/bin/bash

cd "$(dirname "$0")/.."

# 1) Generate filelist (Mac orchestration) — run only when new data arrives
# python tools/build_filelist.py --repo huggingface-datasets/airship-mirror --date 2026-05-03 --out surrogate/data/filelist.json

# 2) Launch training (Lightning)
python surrogate/train.py
```

Make executable:
```bash
chmod +x run_training.sh
```

---

### 5. Crontab / scheduling guard (if used)

If scheduling via cron, ensure environment is correct:
```cron
SHELL=/bin/bash
0 2 * * * cd /opt/axentx/airship && ./run_training.sh >> logs/train.log 2>&1
```

---

## Verification Steps (≤15 min)

```bash
# 1) Generate filelist (once)
python tools/build_filelist.py --repo huggingface-datasets/airship-mirror --date 2026-05-03 --out surrogate/data/filelist.json

# 2) Confirm CDN URLs resolve (no auth)
curl -I "https://huggingface.co/datasets/huggingface
