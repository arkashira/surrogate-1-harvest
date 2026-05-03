# airship / frontend

Below is the **single, merged implementation** that keeps every high-value promise (CDN-only training, HF rate-limit resilience, Lightning idle-resilience, Mac-as-orchestrator, L40S→H200 fallback) while resolving contradictions and maximizing concrete actionability.

---

## 1) Immediate incremental improvement (<2h)

Add three files and one env var.  
Total dev time: ~90–120 min.

- `/opt/axentx/airship/frontend/orchestrate_training.py`  
  Mac-side orchestrator: lists HF dataset once (rate-limit safe), writes `file-list.json`, starts/reuses Lightning Studio, submits job, monitors idle-stop + auto-restart.

- `/opt/axentx/airship/frontend/train_cdn_only.py`  
  Lightning-side training script: CDN-only PyArrow streaming, selects `{prompt,response}` only, checkpointing, Lightning `LightningModule` + `LightningDataModule`, graceful stop on idle.

- `/opt/axentx/airship/frontend/requirements_training.txt`  
  Pinned Lightning + HF + Arrow deps for the Lightning container.

- Environment:  
  `LIGHTNING_CLOUD=lightning-lambda-prod` (H200) with `MACHINE_TYPE=L40S` fallback.

---

## 2) Files (copy/paste ready)

### `/opt/axentx/airship/frontend/requirements_training.txt`
```text
lightning>=2.2
huggingface-hub>=0.22
pyarrow>=14
requests>=2.31
tqdm>=4.66
torch>=2.1
```

---

### `/opt/axentx/airship/frontend/orchestrate_training.py`
```python
#!/usr/bin/env python3
"""
Frontend orchestrator (run on Mac).
- Pre-lists HF dataset files once (rate-limit safe)
- Produces file-list.json for CDN-only training
- Starts/reuses Lightning Studio and submits training job
- Monitors idle-stop and auto-restarts if needed
"""
import json
import os
import sys
import time
import hashlib
import datetime
import subprocess
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree, HfApi
    from lightning import Teamspace, Studio, Machine
except ImportError as e:
    print("Missing dep:", e)
    print("Install: pip install -r requirements_training.txt")
    sys.exit(1)

# ---- Config (override via env) ----
HF_REPO = os.getenv("HF_REPO", "my-org/surrogate-dataset")
HF_FOLDER = os.getenv("HF_FOLDER", "batches/mirror-merged")
OUTPUT_DIR = Path(__file__).parent
FILE_LIST_PATH = OUTPUT_DIR / "file-list.json"
TRAIN_SCRIPT_PATH = OUTPUT_DIR / "train_cdn_only.py"
LIGHTNING_CLOUD = os.getenv("LIGHTNING_CLOUD", "lightning-lambda-prod")  # H200
MACHINE_TYPE = os.getenv("MACHINE_TYPE", "L40S")  # fallback if H200 unavailable
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_WAIT = int(os.getenv("RETRY_WAIT", "60"))
IDLE_RESTART = os.getenv("IDLE_RESTART", "true").lower() == "true"
# ----

api = HfApi()

def slug_to_repo_index(slug: str, n_siblings: int = 5) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % n_siblings

def list_parquet_files() -> list[str]:
    for attempt in range(MAX_RETRIES):
        try:
            items = list_repo_tree(
                repo_id=HF_REPO,
                path=HF_FOLDER.rstrip("/"),
                repo_type="dataset",
                recursive=False,
            )
            files = [
                f.rfilename
                for f in items
                if f.rfilename.lower().endswith(".parquet")
            ]
            print(f"Found {len(files)} parquet files in {HF_REPO}/{HF_FOLDER}")
            return files
        except Exception as e:
            wait = RETRY_WAIT * (attempt + 1)
            print(f"list_repo_tree failed (attempt {attempt+1}/{MAX_RETRIES}): {e}")
            if "429" in str(e):
                print(f"Rate limited — waiting {wait}s")
                time.sleep(wait)
            else:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(RETRY_WAIT)
    return []

def write_file_list(files: list[str]) -> None:
    payload = {
        "repo_id": HF_REPO,
        "folder": HF_FOLDER.rstrip("/"),
        "files": sorted(files),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    FILE_LIST_PATH.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {FILE_LIST_PATH}")

def ensure_train_script() -> None:
    if TRAIN_SCRIPT_PATH.exists():
        print(f"Train script exists: {TRAIN_SCRIPT_PATH}")
        return

    # Minimal CDN-only Lightning template (inline for single-file deploy)
    content = """#!/usr/bin/env python3
import os
import json
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from torch.utils.data import IterableDataset, DataLoader
import lightning as L
import torch
import torch.nn as nn

HF_REPO = os.getenv("HF_REPO", "my-org/surrogate-dataset")
HF_FOLDER = os.getenv("HF_FOLDER", "batches/mirror-merged")
FILE_LIST = os.getenv("FILE_LIST", "file-list.json")
CKPT_DIR = os.getenv("CKPT_DIR", "checkpoints")
os.makedirs(CKPT_DIR, exist_ok=True)

with open(FILE_LIST) as f:
    meta = json.load(f)

FILES = meta["files"]
REPO_ID = meta["repo_id"]
FOLDER = meta["folder"].rstrip("/")

def cdn_url(path):
    return f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{path}"

class CDNParquetIterable(IterableDataset):
    def __init__(self, files):
        self.files = files

    def __iter__(self):
        for fn in self.files:
            url = cdn_url(f"{FOLDER}/{fn}")
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                table = pq.read_table(pa.BufferReader(r.content))
                if "prompt" not in table.column_names or "response" not in table.column_names:
                    continue
                table = table.select(["prompt", "response"])
                for batch in table.to_batches(max_chunksize=1024):
                    d = batch.to_pydict()
                    for i in range(len(d["prompt"])):
                        yield {"prompt": d["prompt"][i], "response": d["response"][i]}

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, batch_size=8, num_workers=2):
        super().__init__()
        self.batch_size = batch_size
        self.num_workers = num_workers

    def train_dataloader(self):
        dataset = CDNParquetIterable(FILES)
        return DataLoader(dataset, batch_size=self.batch_size, num_workers=self.num_workers)

class SurrogateModel(L.LightningModule):
    def __init__(self, lr=1e-4):
        super().__init__()
        self.lr = lr
        # Replace with your real model
        self.net = nn.Sequential(nn.Linear(10, 10), nn.ReLU(), nn.Linear(10, 1))

    def training_step(self, batch, batch_idx):
        # Dummy step — replace with real surrogate training logic
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.lr)

if __name__ == "__main__":
    dm = SurrogateDataModule(batch_size=8, num_workers=2)
    model = SurrogateModel(lr=1e-4)
    trainer = L.Trainer(
        max_epochs=1,
        default_root_dir=CKPT_DIR,
        enable_checkpointing=True,
        log_every_n_steps=
