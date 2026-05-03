# vanguard / quality

## Final Unified Implementation

### 1. Diagnosis (merged)
- **No persisted manifest** per `(repo, dateFolder)` → every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- **Training uses `load_dataset(streaming=True)` on heterogeneous repos** → pyarrow `CastError` on mixed schemas.
- **No CDN-only data path** → training depends on authenticated API calls instead of public CDN URLs.
- **Ingestion writes mixed-schema files with extra metadata columns** instead of projecting to `{prompt, response}` only.
- **Local runs attempt heavy data loading / `from_pretrained` locally** instead of delegating training to Lightning Studio.

### 2. Proposed Change (merged)
Create a **manifest builder + CDN-only training loader** for the surrogate-1 pipeline:
- Add `/opt/axentx/vanguard/scripts/build_manifest.py` — one-shot script that lists a single date-folder via HF API (non-recursive) and writes `manifests/{repo_slug}/{date}.json`.
- Add `/opt/axentx/vanguard/training/train_cdn.py` — Lightning-compatible training script that loads only the JSON manifest and fetches parquet files via public CDN URLs (no auth, no API calls during training).
- **Update ingestion** to project to `{prompt, response}` only and store files as `batches/mirror-merged/{date}/{slug}.parquet`.

### 3. Implementation

```bash
# Create directory structure
mkdir -p /opt/axentx/vanguard/{scripts,training,manifests,batches/mirror-merged}
```

---

#### 3.1 Manifest builder (run on Mac or any machine with HF token)
`/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for one repo + date-folder.
Run once per date-folder. Avoids recursive listing; uses non-recursive tree + CDN URLs.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_REPO", "datasets/axentx/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER")  # e.g. 2026-04-29
OUT_DIR = Path(os.getenv("OUT_DIR", "manifests"))

if not DATE_FOLDER:
    print("Set DATE_FOLDER env var (e.g. 2026-04-29)")
    sys.exit(1)

api = HfApi()

def build_manifest(repo: str, date_folder: str):
    # Non-recursive listing for the date folder only
    entries = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    files = []
    for e in entries:
        if e.type != "file":
            continue
        if not e.path.endswith(".parquet"):
            continue

        # Public CDN URL — no auth required
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{e.path}"
        files.append({
            "repo": repo,
            "path": e.path,
            "cdn_url": cdn_url,
            "size": e.size or 0,
            "lfs": getattr(e, "lfs", None)
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "total_files": len(files),
        "note": "CDN-only manifest. Do not use HF API during training."
    }
    return manifest

def main():
    manifest = build_manifest(HF_REPO, DATE_FOLDER)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = HF_REPO.replace("/", "_")
    out_path = OUT_DIR / f"{slug}_{DATE_FOLDER}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path}")
    print(f"Total files: {manifest['total_files']}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

Usage (on Mac):
```bash
export HF_REPO="datasets/axentx/surrogate-1"
export DATE_FOLDER="2026-04-29"
python3 /opt/axentx/vanguard/scripts/build_manifest.py
```

---

#### 3.2 CDN-only training loader (Lightning)
`/opt/axentx/vanguard/training/train_cdn.py`
```python
#!/usr/bin/env python3
"""
Lightning-compatible training script that uses CDN-only parquet files.
No HF API calls during training. Manifest must be pre-built.
"""
import json
import os
from pathlib import Path
from typing import Dict, List

import lightning as L
import pandas as pd
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM

MANIFEST_PATH = Path(os.getenv("MANIFEST_PATH", "manifests/datasets_axentx_surrogate-1_2026-04-29.json"))
MODEL_NAME = os.getenv("MODEL_NAME", "microsoft/DialoGPT-medium")
MAX_LENGTH = int(os.getenv("MAX_LENGTH", "512"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))
LR = float(os.getenv("LR", "5e-5"))

class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path: Path, tokenizer, max_length: int = 512):
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.examples: List[Dict] = []

        manifest = json.loads(manifest_path.read_text())
        for f in manifest["files"]:
            # CDN fetch — no auth header required
            df = pd.read_parquet(f["cdn_url"])
            # Project to {prompt, response} only (ignore extra cols)
            for _, row in df.iterrows():
                prompt = str(row.get("prompt", row.get("input", "")))
                response = str(row.get("response", row.get("output", "")))
                if not prompt or not response:
                    continue
                self.examples.append({"prompt": prompt, "response": response})

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        ex = self.examples[idx]
        # Simple concatenation for causal LM training
        text = f"Prompt: {ex['prompt']}\nResponse: {ex['response']}\n"
        enc = self.tokenizer(
            text,
            max_length=self.max_length,
            truncation=True,
            padding="max_length",
            return_tensors="pt"
        )
        return {k: v.squeeze(0) for k, v in enc.items()}

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: Path, tokenizer, batch_size: int = 4, max_length: int = 512):
        super().__init__()
        self.manifest_path = manifest_path
        self.tokenizer = tokenizer
        self.batch_size = batch_size
        self.max_length = max_length

    def setup(self, stage=None):
        self.dataset = CDNParquetDataset(self.manifest_path, self.tokenizer, self.max_length)

    def train_dataloader(self):
        return DataLoader(self.dataset, batch_size=self.batch_size, shuffle=True)

class SurrogateModel(L.LightningModule):
    def __init__(self, model_name: str = MODEL_NAME, lr: float = LR):
        super().__init__()
        self.model = AutoModelForCausalLM.from_pretrained(model_name)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.lr = lr
        self.save_hyperparameters(ignore=["model", "tokenizer"])

    def training_step(self, batch, batch_idx):
        outputs = self.model(**batch, labels=batch["input_ids"])
        loss = outputs.loss
        self.log("train_loss", loss, prog_bar=True)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(),
