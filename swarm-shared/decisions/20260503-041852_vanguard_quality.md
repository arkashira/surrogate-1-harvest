# vanguard / quality

## 1. Diagnosis

- No content-addressed manifest exists → training/frontend hit HF API at runtime, causing 429s and non-reproducible epochs.
- Mixed-schema `enriched/` files include `source`/`ts` columns that break `load_dataset` expectations for surrogate-1 training.
- Lightning Studio reuse is not implemented → quota waste (~80hr/mo) and cold-start delays on every run.
- Data loading uses `load_dataset(streaming=True)` on heterogeneous repos → pyarrow `CastError` on schema drift.
- No CDN bypass strategy → every epoch re-authenticates against `/api/` and risks rate limits during long training runs.

## 2. Proposed change

Create a minimal, high-leverage quality shim:

- `/opt/axentx/vanguard/train.py` (new): Lightning training entrypoint that:
  - Accepts a pre-listed `file_list.json` (generated once on Mac).
  - Uses CDN-only URLs (`resolve/main/...`) via `datasets` with `streaming=True` and column projection to `{prompt, response}`.
  - Reuses an existing running Lightning Studio or starts one deterministically.
- `/opt/axentx/vanguard/list_files.py` (new): One-off Mac script to list a date folder and emit `file_list.json`.
- `/opt/axentx/vanguard/.lightning_reuse.py` (new): Helper to find and reuse running studios.

Scope: 3 small files; no changes to existing repo history or CI.

## 3. Implementation

```bash
cd /opt/axentx/vanguard
```

### 3.1 `.lightning_reuse.py` — Studio reuse helper

```python
# /opt/axentx/vanguard/.lightning_reuse.py
import os
from lightning_sdk import Teamspace, Studio, Machine

def get_or_start_studio(
    name: str = "vanguard-train",
    machine: str = "lightning-lambda-prod://L40S",
    idle_timeout: int = 30,
):
    """
    Reuse a running studio or start a new one.
    Returns (studio, was_running: bool)
    """
    teamspace = Teamspace.current()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name} (id={s.id})")
            return s, True

    print(f"No running studio '{name}' found. Starting new on {machine}...")
    studio = Studio(
        name=name,
        machine=machine,
        cloud_account="lightning-lambda-prod",  # H200/L40S available
        idle_timeout=idle_timeout,
    )
    studio.start(machine=machine)
    return studio, False
```

### 3.2 `list_files.py` — One-off Mac script (run once per date folder)

```python
#!/usr/bin/env python3
"""
Mac-only orchestration script.
Run once after HF API rate-limit window clears.
Produces file_list.json for CDN-only training.
"""
import json
import os
import sys
from datetime import datetime

# HF SDK import guarded so this can run on Mac without heavy deps
try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install huggingface_hub on Mac: pip install huggingface_hub")
    sys.exit(1)

REPO = "datasets/your-org/vanguard-enriched"  # adjust
DATE_FOLDER = datetime.utcnow().strftime("%Y-%m-%d")  # or pass via CLI
OUT_PATH = "file_list.json"

def main():
    print(f"Listing {REPO}/{DATE_FOLDER} (non-recursive)...")
    tree = list_repo_tree(REPO, path=DATE_FOLDER, recursive=False)
    files = [
        f"{DATE_FOLDER}/{entry.path.split('/')[-1]}"
        for entry in tree
        if entry.type == "file" and entry.path.endswith(".parquet")
    ]
    payload = {
        "repo": REPO,
        "date_folder": DATE_FOLDER,
        "files": sorted(files),
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
    }
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {OUT_PATH}")

if __name__ == "__main__":
    main()
```

Make it executable (Mac orchestration rule):

```bash
chmod +x list_files.py
```

### 3.3 `train.py` — Lightning training entrypoint (quality-focused)

```python
# /opt/axentx/vanguard/train.py
import json
import os
from pathlib import Path

import torch
from datasets import load_dataset
from lightning import Fabric
from lightning.pytorch import Trainer
from lightning.pytorch.strategies import FSDPStrategy

from ._lightning_reuse import get_or_start_studio

# ----------------------------
# Config (keep minimal)
# ----------------------------
HF_REPO = os.getenv("HF_REPO", "datasets/your-org/vanguard-enriched")
FILE_LIST = os.getenv("FILE_LIST", "file_list.json")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "1000"))
OUT_DIR = Path(os.getenv("OUT_DIR", "checkpoints"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------------
# CDN-only dataset loader
# ----------------------------
def build_cdn_dataloader(file_list_path: str, repo: str, batch_size: int):
    with open(file_list_path) as f:
        manifest = json.load(f)

    # Build CDN URLs (no Authorization header required)
    base = f"https://huggingface.co/{repo}/resolve/main"
    urls = [f"{base}/{f}" for f in manifest["files"]]

    # Project to {prompt, response} at parse time; ignore source/ts
    ds = load_dataset(
        "parquet",
        data_files={"train": urls},
        streaming=True,
        split="train",
    )

    # Select only required columns; schema-drift safe
    ds = ds.select_columns(["prompt", "response"])

    def tokenize_and_mask(ex):
        # Placeholder: replace with real tokenizer logic
        # Keep lightweight here; tokenizer should run on GPU via Fabric
        return {
            "input_ids": ex["prompt"],   # mock
            "labels": ex["response"],    # mock
        }

    ds = ds.map(tokenize_and_mask, remove_columns=["prompt", "response"])

    dl = torch.utils.data.DataLoader(
        ds,
        batch_size=batch_size,
        num_workers=0,  # streaming + Lightning handles workers
        collate_fn=lambda x: {
            "input_ids": torch.stack([torch.tensor(v["input_ids"]) for v in x]),
            "labels": torch.stack([torch.tensor(v["labels"]) for v in x]),
        },
    )
    return dl

# ----------------------------
# Minimal surrogate-1 model stub
# ----------------------------
class Surrogate1(torch.nn.Module):
    def __init__(self, vocab_size=32000, d_model=1024):
        super().__init__()
        self.embed = torch.nn.Embedding(vocab_size, d_model)
        self.ln = torch.nn.LayerNorm(d_model)
        self.head = torch.nn.Linear(d_model, vocab_size)

    def forward(self, input_ids, labels=None):
        x = self.embed(input_ids)
        x = self.ln(x)
        logits = self.head(x)
        loss = None
        if labels is not None:
            loss = torch.nn.functional.cross_entropy(
                logits.view(-1, logits.size(-1)), labels.view(-1)
            )
        return {"logits": logits, "loss": loss}

class Surrogate1LitModule(torch.nn.Module):
    # Lightweight Lightning-compatible wrapper
    def __init__(self):
        super().__init__()
        self.model = Surrogate1()

    def training_step(self, batch, batch_idx):
        out = self.model(batch["input_ids"], labels=batch["labels"])
        self.log("train_loss", out["loss"], prog_bar=True)
        return out["loss"]

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=1e-4)

# ----------------------------
#
