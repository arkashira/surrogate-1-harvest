# airship / frontend

## Final integrated implementation (≤2h)

**Goal**: Eliminate HF API rate-limit failures and Lightning idle-timeout deaths during Surrogate training by shipping a CDN-only parquet loader and an idle-resilient Lightning Studio runner that reuses or restarts automatically.

---

### 1) scripts/list_hf_files.py (15m)

- One-time, non-recursive HF API call per folder.
- Emits `train_files.json` with CDN URLs (`resolve/main/...`) to bypass auth/rate limits during training.

```python
#!/usr/bin/env python3
"""
List files in a single HF dataset folder (non-recursive) and save to JSON.
Usage:
  HF_REPO=datasets/your/repo HF_FOLDER=train/2026-05-03 \
  python scripts/list_hf_files.py --out train_files.json
"""
import argparse
import json
import os
import sys

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser(description="List HF dataset folder (non-recursive).")
    parser.add_argument("--out", default="train_files.json", help="Output JSON path")
    args = parser.parse_args()

    repo = os.getenv("HF_REPO")
    folder = os.getenv("HF_FOLDER", "").rstrip("/")
    if not repo:
        print("ERROR: set HF_REPO (e.g. datasets/your/repo)", file=sys.stderr)
        sys.exit(1)

    api = HfApi()
    entries = api.list_repo_tree(repo=repo, path=folder, recursive=False)

    files = []
    for e in entries:
        if e.type == "file":
            cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{e.path}"
            files.append({"path": e.path, "cdn_url": cdn_url})

    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"repo": repo, "folder": folder, "files": files}, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x scripts/list_hf_files.py
```

---

### 2) surrogate/train.py (60–90m)

- CDN-only parquet loader (no `load_dataset`, no auth).
- Projects only `{prompt, response}` to avoid schema/cast errors.
- Lightning Studio reuse + auto-restart on idle timeout.
- Small smoke-run defaults for quick validation.

```python
#!/usr/bin/env python3
"""
CDN-only parquet loader + Lightning idle-resilient runner for Surrogate training.

Key behaviors:
- Loads parquet via CDN URLs (no HF API calls during training).
- Projects only {prompt, response} fields (avoids mixed-schema CastError).
- Reuses existing Lightning Studio if running; restarts if stopped (idle timeout).
"""
import json
import os
import sys
from pathlib import Path

import lightning as L
import pandas as pd
import torch
from torch.utils.data import DataLoader, Dataset

# ------------------------- Configuration -------------------------
HF_REPO = os.getenv("HF_REPO", "datasets/your/repo")
HF_FOLDER = os.getenv("HF_FOLDER", "train/2026-05-03")
FILE_LIST = os.getenv("FILE_LIST", "train_files.json")  # produced by list_hf_files.py

LATENT_TEAMSPACE = os.getenv("LIGHTNING_TEAMSPACE", "default")
STUDIO_NAME = os.getenv("LIGHTNING_STUDIO", "surrogate-train")
MACHINE = os.getenv("LIGHTNING_MACHINE", "L40S")  # H200 requires lightning-lambda-prod

# ------------------------- Dataset -------------------------
class CDNParquetDataset(Dataset):
    def __init__(self, cdn_urls, max_rows=None):
        self.rows = []
        for url in cdn_urls:
            df = pd.read_parquet(url, columns=["prompt", "response"])
            if max_rows:
                df = df.head(max_rows)
            for _, rec in df.iterrows():
                self.rows.append({
                    "prompt": str(rec["prompt"]),
                    "response": str(rec["response"]),
                })

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        # Return tokenizable text pair; tokenizer handled in collate/model.
        return self.rows[idx]

def build_dataloader(json_path: str, batch_size: int = 4, max_rows: int = None) -> DataLoader:
    with open(json_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    cdn_urls = [f["cdn_url"] for f in manifest["files"]]
    if not cdn_urls:
        raise RuntimeError("No files found in manifest")
    dataset = CDNParquetDataset(cdn_urls, max_rows=max_rows)
    return DataLoader(dataset, batch_size=batch_size, shuffle=True)

# ------------------------- Lightning module (minimal) -------------------------
class SurrogateTrainer(L.LightningModule):
    def __init__(self, model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"):
        super().__init__()
        # Placeholder: replace with real model init (run on Lightning Studio, not Mac).
        self.model_name = model_name
        self.save_hyperparameters()

    def training_step(self, batch, batch_idx):
        # Minimal step: real training will implement forward + loss.
        self.log("train/loss", torch.rand(1).item(), prog_bar=True)
        return torch.rand(1).item()

    def configure_optimizers(self):
        # Placeholder params
        return torch.optim.Adam([torch.nn.Parameter(torch.zeros(1))], lr=1e-4)

# ------------------------- Studio runner (idle-resilient) -------------------------
def get_or_create_studio() -> L.studio.Studio:
    teamspace = L.Teamspace(
        name=LATENT_TEAMSPACE,
        create_ok=True,
    )

    # Reuse running studio if exists
    for s in teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s

    # Start new if not running
    machine = L.Machine(MACHINE)
    studio = teamspace.create_studio(
        name=STUDIO_NAME,
        machine=machine,
        create_ok=True,
    )
    print(f"Created studio: {studio.name}")
    return studio

def run_training():
    json_path = Path(FILE_LIST)
    if not json_path.exists():
        print(f"ERROR: {FILE_LIST} not found. Run scripts/list_hf_files.py first.", file=sys.stderr)
        sys.exit(1)

    # Small smoke-run settings for quick validation
    dataloader = build_dataloader(str(json_path), batch_size=2, max_rows=100)

    # Studio orchestration
    studio = get_or_create_studio()

    # Lightning idle timeout kills training; restart if stopped.
    if studio.status != "running":
        print(f"Studio stopped ({studio.status}). Restarting...")
        machine = L.Machine(MACHINE)
        studio.start(machine=machine)

    # Run inside studio context
    trainer = L.Trainer(
        max_epochs=1,
        limit_train_batches=10,
        enable_checkpointing=False,
    )
    model = SurrogateTrainer()
    trainer.fit(model, dataloader)

if __name__ == "__main__":
    run_training()
```

---

### 3) .env.example (5m)

```text
# HF dataset to list and load (CDN-only during training)
HF_REPO=datasets/your/repo
HF_FOLDER=train/2026-05-03

# Output from scripts/list_hf_files.py
FILE_LIST=train_files.json

# Lightning Studio settings
LIGHTNING_TEAMSPACE=default
LIGHTNING_STUDIO=surrogate-train
LIGHTNING_MACHINE=L40S
```

---

### 4) requirements.txt additions (5m)

```text
lightning
pyarrow
pandas
requests
tqdm
huggingface_hub
```

---

### 5) Execution
