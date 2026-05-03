# vanguard / backend

## Final Synthesis (Corrected + Actionable)

**Core diagnosis (merged):**  
- Training still uses authenticated HF API (`load_dataset`, `list_repo_tree`) during data loading → quota burn + 429 risk.  
- No static file manifest; every run re-enumerates repo via API.  
- No CDN bypass; fetches go through `/api/` instead of public `resolve/main/` URLs.  
- No retry/backoff for 429s and no automatic fallback to CDN.  
- Mac orchestration must remain CLI-only; training must run on Lightning Studio, not locally on Mac.  
- No studio reuse logic; scripts likely recreate studios and waste Lightning quota.

**Target:**  
- `/opt/axentx/vanguard` backend orchestration and training entrypoint.  
- Deliver:  
  1. One-time Mac manifest generator (run after rate-limit window).  
  2. `train.py` that embeds the manifest, uses CDN-only fetches, retries with exponential backoff, falls back to CDN on 429, and reuses a running Lightning Studio.

---

## Implementation

### 1) Generate static file manifest (run once on Mac)

```bash
# /opt/axentx/vanguard/scripts/gen_manifest.py
#!/usr/bin/env python3
"""
Generate static file manifest for a date folder in HF dataset repo.
Run from Mac when API quota is available. Embed output in train.py or place alongside it.
"""
import json
import os
import sys
from huggingface_hub import HfApi

REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")  # e.g. batches/mirror-merged/2026-04-29
OUT_PATH = os.getenv("OUT_MANIFEST", "file_manifest.json")

def main() -> None:
    api = HfApi()
    # Single non-recursive call to list folder contents
    tree = api.list_repo_tree(repo_id=REPO_ID, path=DATE_FOLDER, recursive=False)
    files = sorted(
        f.rfilename
        for f in tree
        if f.rfilename.endswith(".parquet") and f.rfilename.startswith(DATE_FOLDER + "/")
    )
    manifest = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "files": files,
        "total_files": len(files),
        "cdn_base": f"https://huggingface.co/datasets/{REPO_ID}/resolve/main",
    }
    os.makedirs(os.path.dirname(os.path.abspath(OUT_PATH)), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {OUT_PATH}")

if __name__ == "__main__":
    main()
```

Run once:
```bash
chmod +x /opt/axentx/vanguard/scripts/gen_manifest.py
cd /opt/axentx/vanguard
python3 scripts/gen_manifest.py
test -f file_manifest.json && echo "Manifest OK"
```

---

### 2) Training script: CDN-only fetches, retry/backoff, fallback, studio reuse

```python
# /opt/axentx/vanguard/train.py
#!/usr/bin/env python3
"""
Lightning training with CDN-bypass, retry/backoff, 429 fallback, and studio reuse.
Mac runs this script for orchestration only. Training executes on Lightning Studio.
"""
import json
import os
import sys
import time
import random
from pathlib import Path

import pyarrow.parquet as pq
import requests
import lightning as L
import torch
from lightning.pytorch.utilities import rank_zero_only
from torch.utils.data import IterableDataset, DataLoader

# Embedded or adjacent manifest
MANIFEST_PATH = Path(__file__).parent / "file_manifest.json"
assert MANIFEST_PATH.exists(), f"Missing {MANIFEST_PATH}. Run gen_manifest.py first."

with open(MANIFEST_PATH) as f:
    MANIFEST = json.load(f)

CDN_BASE = MANIFEST["cdn_base"]
FILE_LIST = MANIFEST["files"]

# Retry policy
MAX_RETRIES = 5
BASE_DELAY = 1.0
MAX_DELAY = 60.0

def cdn_fetch_parquet(rel_path: str):
    """
    Fetch parquet via CDN with exponential backoff.
    On 429, retries with backoff; on persistent failure, raises.
    """
    url = f"{CDN_BASE}/{rel_path}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 429:
                # Fallback: retry with backoff (no auth header)
                delay = min(BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1), MAX_DELAY)
                print(f"429 on {url} (attempt {attempt}). Backing off {delay:.1f}s")
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return pq.read_table(pq.ParquetFile(pq.ParquetReader(pq.BufferReader(resp.content))))
        except (requests.RequestException, OSError) as exc:
            delay = min(BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1), MAX_DELAY)
            print(f"Error fetching {url} (attempt {attempt}): {exc}. Retrying in {delay:.1f}s")
            time.sleep(delay)
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts")

def load_dataset_cdn():
    """Yield {prompt, response} rows from parquet files via CDN."""
    for rel in FILE_LIST:
        table = cdn_fetch_parquet(rel)
        if "prompt" in table.column_names and "response" in table.column_names:
            df = table.select(["prompt", "response"]).to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"]}

class SurrogateDataModule(L.LightningDataModule):
    def train_dataloader(self):
        class CdnIterable(IterableDataset):
            def __iter__(self):
                return load_dataset_cdn()
        return DataLoader(CdnIterable(), batch_size=8, num_workers=0)

class SurrogateModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        # Replace with real model
        self.lm = torch.nn.Linear(10, 1) if False else None

    def training_step(self, batch, batch_idx):
        # Placeholder
        loss = batch_idx * 0.01 + 0.5
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

@rank_zero_only
def reuse_or_create_studio():
    """
    Reuse running studio if exists; else create with L40S (free-tier friendly).
    Avoids recreating studios and wasting Lightning quota.
    """
    teamspace = L.Teamspace()
    studio_name = "vanguard-surrogate-train"
    running = [s for s in teamspace.studios if s.name == studio_name and getattr(s, "status", None) == "Running"]
    if running:
        print(f"Reusing running studio: {running[0].id}")
        return running[0]

    from lightning.pytorch.cloud import Machine
    machine = Machine.L40S
    studio = teamspace.create_studio(name=studio_name, machine=machine, create_ok=True)
    print(f"Created studio {studio.id} on {machine}")
    return studio

def main():
    # Mac orchestration: reuse/create studio and submit run
    studio = reuse_or_create_studio()

    if getattr(studio, "status", None) != "Running":
        print(f"Studio stopped ({studio.status}). Restarting...")
        from lightning.pytorch.cloud import Machine
        studio.start(machine=Machine.L40S)

    # Submit training run to studio (avoid re-orchestrating inside studio)
    run = studio.run(
        cloud_build_config={
            "requirements": ["torch", "pyarrow", "requests", "lightning"],
        },
        entry_point="train.py",
        arguments
