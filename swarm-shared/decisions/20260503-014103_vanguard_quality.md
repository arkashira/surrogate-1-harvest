# vanguard / quality

## 1. Diagnosis

- Repeated authenticated `list_repo_tree` calls on every training/data-load burn the HF API quota (1000/5min) and cause intermittent 429s.
- No persisted `(repo, dateFolder) → file-list` manifest exists, forcing re-enumeration and preventing reproducible, CDN-only training runs.
- Training/data loader likely uses `load_dataset(streaming=True)` or authenticated SDK calls during epoch loops, mixing API calls with CDN fetches.
- No guardrails to reuse running Lightning Studio instances, risking quota waste and idle-stop training loss.
- Missing deterministic repo-selection for commit-cap avoidance when mirroring/writing enriched parquet files.

## 2. Proposed change

Add a lightweight manifest generator and CDN-only data loader for the surrogate-1 pipeline:

- File: `/opt/axentx/vanguard/data/manifest.py` (new)
- File: `/opt/axentx/vanguard/train/train.py` (modify data-loading section)
- File: `/opt/axentx/vanguard/scripts/gen_manifest.sh` (new, executable)
- File: `/opt/axentx/vanguard/train/lightning_launcher.py` (add studio-reuse + idle-check)

Scope: ~200 lines total; focused on quality/reliability, no model changes.

## 3. Implementation

### 3.1 Manifest generator (CDN-bypass strategy)

`/opt/axentx/vanguard/data/manifest.py`
```python
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from huggingface_hub import list_repo_tree

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/surrogate-1")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", "")  # e.g. "2026-04-29"
OUT_PATH = Path(os.getenv("MANIFEST_OUT", "data/manifest.json"))

def build_manifest(repo: str, date_folder: str, out: Path) -> None:
    if not date_folder:
        print("HF_DATE_FOLDER is required", file=sys.stderr)
        sys.exit(1)

    # Single API call (non-recursive) to list one date folder
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [it.rfilename for it in items if it.type == "file" and it.rfilename.endswith(".parquet")]

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": sorted(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}",
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out} ({len(files)} files)")

if __name__ == "__main__":
    build_manifest(HF_REPO, DATE_FOLDER, OUT_PATH)
```

`/opt/axentx/vanguard/scripts/gen_manifest.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

cd /opt/axentx/vanguard
python -m data.manifest
```

```bash
chmod +x /opt/axentx/vanguard/scripts/gen_manifest.sh
```

### 3.2 CDN-only dataset loader (zero API during training)

`/opt/axentx/vanguard/train/train.py` (diff snippet)
```diff
+ import json
+ from pathlib import Path
+ import pyarrow.parquet as pq
+ import torch
+ from torch.utils.data import IterableDataset, DataLoader
+
+ class CDNParquetDataset(IterableDataset):
+     def __init__(self, manifest_path: str | Path):
+         manifest = json.loads(Path(manifest_path).read_text())
+         self.prefix = manifest["cdn_prefix"]
+         self.files = manifest["files"]
+
+     def __iter__(self):
+         for fname in self.files:
+             url = f"{self.prefix}/{fname}"
+             # stream remote parquet without auth (CDN bypass)
+             tbl = pq.read_table(url, memory_map=False)
+             df = tbl.to_pandas()
+             for _, row in df.iterrows():
+                 yield {"prompt": row["prompt"], "response": row["response"]}
+
+ def make_dataloader(manifest_path: str | Path, batch_size: int = 8, num_workers: int = 2):
+     dataset = CDNParquetDataset(manifest_path)
+     return DataLoader(dataset, batch_size=batch_size, num_workers=num_workers, pin_memory=True)
+
+ # Usage in training loop:
+ # loader = make_dataloader("data/manifest.json")
```

### 3.3 Lightning Studio reuse + idle-stop guard

`/opt/axentx/vanguard/train/lightning_launcher.py` (add/modify)
```python
from lightning import Fabric, LightningFabric, Machine
from lightning.fabric.plugins import LightningStudio
from typing import Optional

def get_or_create_studio(name: str, machine: Machine = Machine.L40S) -> LightningStudio:
    from lightning import Teamspace
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return LightningStudio(create_ok=True, name=name, machine=machine)

def run_with_idle_guard(studio: LightningStudio, target):
    if studio.status != "Running":
        print("Studio not running; restarting...")
        studio.start(machine=studio.machine or Machine.L40S)
    studio.run(target)
```

### 3.4 Deterministic repo selection for commit-cap avoidance (mirror/writer)

`/opt/axentx/vanguard/ingest/mirror.py` (add helper)
```python
import hashlib

def pick_sibling_repo(slug: str, siblings: list[str]) -> str:
    """Deterministic repo selection to spread HF commit load."""
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(siblings)
    return siblings[idx]

# Example:
# repos = ["surrogate-1", "surrogate-1-sib1", "surrogate-1-sib2", "surrogate-1-sib3", "surrogate-1-sib4"]
# target_repo = pick_sibling_repo(slug, repos)
```

## 4. Verification

1. Generate manifest (single API call):
   ```bash
   export HF_DATASET_REPO=datasets/surrogate-1
   export HF_DATE_FOLDER=2026-04-29
   ./scripts/gen_manifest.sh
   ```
   Confirm `data/manifest.json` exists and lists parquet files with valid CDN URLs.

2. Validate CDN-only loader (no auth/API during iteration):
   ```python
   from train.train import make_dataloader
   loader = make_dataloader("data/manifest.json", batch_size=2)
   batch = next(iter(loader))
   assert "prompt" in batch and "response" in batch
   print("OK: CDN loader produced batch")
   ```

3. Confirm no HF API calls during epoch loop:
   - Run loader for a few iterations while monitoring `~/.cache/huggingface/*.log` or network; no `api/` requests should appear after manifest generation.

4. Studio reuse/idle guard:
   - Start a dummy target function, stop the studio manually, then invoke `run_with_idle_guard`; verify it restarts and runs without quota waste.

5. Commit-cap helper:
   ```python
   from ingest.mirror import pick_sibling_repo
   repos = ["surrogate-1", "surrogate-1-sib1", "surrogate-1-sib2", "surrogate-1-sib3", "surrogate-1-sib4"]
   assert pick_sibling_repo("fixed-slug", repos) == pick_sibling_repo("fixed-slug", repos)
   print("Deterministic selection OK")
   ```
