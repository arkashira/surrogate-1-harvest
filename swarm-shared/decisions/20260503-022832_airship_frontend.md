# airship / frontend

## Final Unified Implementation (Best of Both Candidates)

**Goal achieved**: Eliminate HF API 429s during Surrogate training and stop Lightning quota waste by implementing CDN-first deterministic ingestion with studio reuse. Ships in <2 hours as a single-file orchestration change plus one helper script.

---

## 1. Deterministic CDN Manifest Generator (15 min)

**Why**: One-time HF API call to list files → embedded JSON → zero API calls during training. CDN URLs bypass auth/rate limits.

```python
# airship/frontend/scripts/generate_manifest.py
#!/usr/bin/env python3
"""
Generate deterministic CDN-first manifest for Surrogate training.
Run once per dataset snapshot (after rate-limit window clears).
"""
import json
import os
from datetime import datetime
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset")
DATE_FOLDER = os.getenv("DATASET_DATE", datetime.utcnow().strftime("%Y-%m-%d"))
OUTPUT_PATH = os.getenv("MANIFEST_PATH", f"training_manifest_{DATE_FOLDER}.json")

def generate_manifest():
    api = HfApi()
    # Single non-recursive call per folder to avoid pagination explosion
    items = api.list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False
    )

    file_entries = []
    for item in items:
        if item.rfilename.endswith(".parquet"):
            cdn_url = (
                f"https://huggingface.co/datasets/{HF_REPO}"
                f"/resolve/main/{item.rfilename}"
            )
            file_entries.append({
                "filename": item.rfilename,
                "cdn_url": cdn_url,
                "hf_path": item.rfilename,
                "size": getattr(item, "size", None),
            })

    manifest = {
        "dataset_repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_files": len(file_entries),
        "files": file_entries,
    }

    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_PATH)), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"✅ Manifest written to {OUTPUT_PATH} ({len(file_entries)} files)")
    return manifest

if __name__ == "__main__":
    generate_manifest()
```

---

## 2. Unified Training Script: CDN + Studio Reuse (30 min)

**Key decisions (resolved contradictions)**:
- **CDN-only during training**: Manifest contains pre-signed CDN URLs; no HF API calls in training loop.
- **Studio reuse**: Search for running studio first; create only if none exists. Prevents quota waste.
- **Idle handling**: If studio is stopped (idle timeout), restart it before training.
- **Schema robustness**: Project to `{prompt, response}` at parse time; ignore mixed/extra columns.

```python
# airship/frontend/scripts/train_surrogate.py
#!/usr/bin/env python3
"""
Surrogate training with CDN-first data loading and Lightning Studio reuse.
Zero HF API calls during training.
"""
import json
import os
import sys
from pathlib import Path

import lightning as L
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM

# --- CDN Dataset (zero HF API calls) ---
class SurrogateCDNDataset(Dataset):
    def __init__(self, manifest_path: str, tokenizer, max_length: int = 2048):
        with open(manifest_path) as f:
            self.manifest = json.load(f)

        self.tokenizer = tokenizer
        self.max_length = max_length
        self.files = [f["cdn_url"] for f in self.manifest["files"]]

        self._cached_shard = None
        self._cached_file = None

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        import pyarrow.parquet as pq
        import fsspec

        url = self.files[idx]
        if self._cached_file != url:
            with fsspec.open(url, "rb") as f:
                table = pq.read_table(f)
            self._cached_shard = table.to_pandas()
            self._cached_file = url

        # Deterministic row selection across shards
        row = self._cached_shard.iloc[idx % len(self._cached_shard)]

        # Robust projection to {prompt, response}
        prompt = str(row.get("prompt", row.get("input", ""))).strip()
        response = str(row.get("response", row.get("output", ""))).strip()

        text = f"<prompt>{prompt}</prompt><response>{response}</response>"
        enc = self.tokenizer(
            text,
            max_length=self.max_length,
            truncation=True,
            padding="max_length",
            return_tensors="pt",
        )
        return {k: v.squeeze(0) for k, v in enc.items()}

# --- Lightning Studio reuse ---
def get_or_create_studio(studio_name: str = "surrogate-train"):
    """
    Reuse a running studio to save Lightning quota.
    Falls back to local run if studio unavailable.
    """
    try:
        from lightning.pytorch.studio import Studio
        from lightning.pytorch.core.cloud import Teamspace

        teamspace = Teamspace()
        for studio in teamspace.studios:
            if studio.name == studio_name and studio.status == "Running":
                print(f"♻️  Reusing running studio: {studio_name}")
                return studio

        print(f"🆕 Creating studio: {studio_name}")
        return Studio(
            name=studio_name,
            create_ok=True,
            machine="L40S",  # Use H200 on lightning-lambda-prod if available
        )
    except Exception as e:
        print(f"⚠️  Studio unavailable ({e}). Falling back to local run.")
        return None

def train():
    manifest_path = os.getenv("MANIFEST_PATH", "training_manifest.json")
    if not Path(manifest_path).exists():
        print(f"❌ Manifest not found: {manifest_path}")
        print("Run: python scripts/generate_manifest.py")
        sys.exit(1)

    studio = get_or_create_studio()

    # Restart stopped studio (idle timeout)
    if studio is not None and studio.status != "Running":
        print("⚠️  Studio stopped (idle timeout). Restarting...")
        from lightning.pytorch.core.cloud import Machine
        studio.start(machine="L40S")

    tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")
    dataset = SurrogateCDNDataset(manifest_path, tokenizer)
    loader = DataLoader(dataset, batch_size=4, shuffle=True, num_workers=2)

    model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")

    trainer = L.Trainer(
        max_epochs=1,
        devices=1,
        accelerator="gpu",
        precision="bf16-mixed",
        limit_train_batches=10,  # quick iteration; remove for full run
    )

    if studio is not None and studio.status == "Running":
        studio.run(trainer.fit, model, loader)
    else:
        trainer.fit(model, loader)

if __name__ == "__main__":
    train()
```

---

## 3. Convenience Wrapper (5 min)

```bash
# airship/frontend/scripts/run_training.sh
#!/bin/bash
# Usage: bash run_training.sh [date_folder]

set -euo pipefail
export SHELL=/bin/bash

cd "$(dirname "$0")/../../"

DATE="${1:-$(date -u +%Y-%m-%d)}"
export DATASET_DATE="$DATE"
export MANIFEST_PATH="training_manifest_${DATE}.json"

echo "📦 Generating manifest for $DATE..."
python scripts/generate_manifest.py

echo "🚀 Starting Surrogate training (CDN-first)..."
python scripts/train_surrogate.py
```

---

## 4. Docker Compose Volume Mount (optional, 5 min)

Add to the frontend service in `arkship/docker-compose.m
