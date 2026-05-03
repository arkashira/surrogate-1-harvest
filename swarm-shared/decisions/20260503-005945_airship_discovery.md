# airship / discovery

## Final Unified Implementation Plan  
*Best parts merged; contradictions resolved in favor of correctness + concrete actionability.*

**Goal**  
Enable Surrogate-1 training in Lightning Studio with **zero Hugging Face API calls during training** by pre-generating a file manifest on the Mac orchestrator and using CDN-only (`resolve/main/`) URLs.  
This eliminates 429 rate limits, avoids quota waste, and sidesteps mixed-schema `pyarrow` issues by projecting only `{prompt,response}` at parse time.

**Time budget:** ~90 minutes

---

### 1. Manifest Generator (Mac orchestrator) — 20 min  
*Single HF API call per run; non-recursive folder listing to avoid pagination overhead.*

`scripts/generate_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate file_manifest.json for one date folder.
Run when HF API rate-limit window is clear.
"""
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUTPUT = os.getenv("MANIFEST_OUT", "file_manifest.json")

def main() -> None:
    api = HfApi()
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=f"batches/mirror-merged/{DATE_FOLDER}",
        repo_type="dataset",
        recursive=False,
    )
    files = sorted(
        e.rfilename
        for e in entries
        if e.rfilename.endswith(".parquet")
    )
    if not files:
        print(f"No parquet files found for {DATE_FOLDER}", file=sys.stderr)
        sys.exit(1)

    manifest = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "files": files,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT)), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {OUTPUT}")

if __name__ == "__main__":
    main()
```

Usage
```bash
chmod +x scripts/generate_manifest.py
DATE_FOLDER=2026-04-29 python3 scripts/generate_manifest.py
```

---

### 2. CDN-Only Data Loader (Lightning training script) — 30 min  
*Zero HF API calls during training; robust schema projection; deterministic caching.*

`surrogate/train.py`
```python
#!/usr/bin/env python3
"""
Surrogate-1 training entrypoint (Lightning Studio).
Uses CDN-only fetches; zero HF API calls during data loading.
"""
import json
import os
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from io import BytesIO
from pathlib import Path
from typing import Dict, List

import torch
from torch.utils.data import Dataset, DataLoader
import lightning as L

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "file_manifest.json")
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/tmp/surrogate_cdn_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.repo_id = self.manifest["repo_id"]
        self.file_urls = [
            f"https://huggingface.co/datasets/{self.repo_id}/resolve/main/{path}"
            for path in self.manifest["files"]
        ]
        self._cache: Dict[str, pa.Table] = {}

    def _download_cached(self, url: str) -> bytes:
        cache_key = url.split("/resolve/main/")[-1].replace("/", "_")
        cache_file = CACHE_DIR / cache_key
        if cache_file.exists():
            return cache_file.read_bytes()
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        cache_file.write_bytes(resp.content)
        return resp.content

    def __len__(self) -> int:
        return len(self.file_urls)

    def _load_table(self, idx: int) -> pa.Table:
        url = self.file_urls[idx]
        if url in self._cache:
            return self._cache[url]
        data = self._download_cached(url)
        table = pq.read_table(BytesIO(data))
        self._cache[url] = table
        return table

    def __getitem__(self, idx: int) -> Dict[str, str]:
        table = self._load_table(idx)
        # Project only {prompt,response}; ignore mixed-schema extras
        prompts = table.column("prompt").to_pylist()
        responses = table.column("response").to_pylist()
        # Return first row per file (extend to yield all rows if needed)
        return {"prompt": prompts[0], "response": responses[0]}

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: str, batch_size: int = 8):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size

    def setup(self, stage: str = None):
        self.dataset = CDNParquetDataset(self.manifest_path)

    def train_dataloader(self):
        return DataLoader(self.dataset, batch_size=self.batch_size, shuffle=True)

class SurrogateModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.lm = torch.nn.Linear(1024, 1024)  # placeholder

    def training_step(self, batch, batch_idx):
        # Replace with real model forward
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

def main():
    dm = SurrogateDataModule(manifest_path=MANIFEST_PATH)
    model = SurrogateModel()
    trainer = L.Trainer(
        max_epochs=1,
        accelerator="gpu" if torch.cuda.is_available() else "cpu",
        devices=1,
        enable_checkpointing=False,
    )
    trainer.fit(model, dm)

if __name__ == "__main__":
    main()
```

---

### 3. Launcher with Studio Reuse — 15 min  
*Reuse a running Lightning Studio to save quota; upload manifest + script; restart only if stopped.*

`scripts/launch_training.py`
```python
#!/usr/bin/env python3
"""
Launch Surrogate-1 training in Lightning AI Studio.
Reuses existing running studio to save quota.
"""
import os
import sys
import time

from lightning_sdk import Client

LIGHTNING_EMAIL = os.getenv("LIGHTNING_EMAIL")
LIGHTNING_PASS = os.getenv("LIGHTNING_PASS")
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "file_manifest.json")
STUDIO_NAME = "surrogate-training-studio"

def main() -> None:
    client = Client(email=LIGHTNING_EMAIL, password=LIGHTNING_PASS)
    teamspace = client.teamspace(name="default")

    studio = None
    for s in teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "running":
            studio = s
            print(f"Reusing running studio: {STUDIO_NAME}")
            break

    if studio is None:
        print(f"Creating new studio: {STUDIO_NAME}")
        studio = teamspace.studios.create(
            name=STUDIO_NAME,
            machine="L40S",
        )
        while studio.status != "running":
            time.sleep(10)
            studio = teamspace.studios.get(STUDIO_NAME)

    # Upload manifest and training script
    studio.upload_file(MANIFEST_PATH, MANIFEST_PATH)
    studio.upload_file("surrogate/train.py", "train.py")

    # Ensure running (restart if stopped)
    if studio.status != "running":
        studio.start(machine="L40S")

    print("Studio ready. Run training with:
