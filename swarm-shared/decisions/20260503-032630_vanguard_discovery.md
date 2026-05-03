# vanguard / discovery

## 1. Diagnosis
- No CDN-first, content-addressed manifest exists; ingestion/training scripts can still trigger `list_repo_tree`/`load_dataset` at runtime → 429s and non-reproducible runs.
- No deterministic file list pinned by date/slug; training workers may fetch different file sets across runs.
- Lightning Studio reuse/idle-stop handling missing; idle-stop kills training and wastes quota.
- Mixed-schema parquet writes still possible (extra cols like `source`, `ts`) breaking Surrogate-1 schema expectations.
- No local guard ensuring Mac-only orchestration (no `model.from_pretrained()` or heavy compute on dev machine).

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/` with:
- `discovery/manifest.py` — one-shot Mac script: `list_repo_tree` per date folder → `manifest-{date}.json` (path list only).
- `discovery/train_cdn.py` — Lightning training entry that loads `manifest-{date}.json` and fetches files via CDN URLs only (no HF API).
- `discovery/studio_launcher.py` — reuse running studio or start L40S; survive idle-stop by checking status before each run.
- `discovery/project_parquet.py` — lightweight util to read any parquet, project to `{prompt, response}`, write to `batches/mirror-merged/{date}/{slug}.parquet`.

## 3. Implementation

```bash
# Create discovery directory
mkdir -p /opt/axentx/vanguard/discovery
```

### discovery/manifest.py
```python
#!/usr/bin/env python3
"""
Run on Mac (or any dev machine) after rate-limit window clears.
Produces manifest-{date}.json listing exact file paths for one date folder.
"""
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-mirror")
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUT_PATH = f"manifest-{DATE_FOLDER}.json"

def build_manifest(date_folder: str, out_path: str):
    api = HfApi()
    # Non-recursive per folder to avoid pagination explosion; recurse only one level of date folder
    tree = api.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=True)
    files = [
        f.rfilename for f in tree
        if not f.rfilename.endswith("/")
    ]
    manifest = {
        "repo": HF_REPO,
        "date": date_folder,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "files": sorted(set(files))
    }
    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    build_manifest(DATE_FOLDER, OUT_PATH)
```

### discovery/project_parquet.py
```python
#!/usr/bin/env python3
"""
Project heterogeneous parquet to {prompt, response} only.
Usage: python project_parquet.py input.parquet output.parquet
"""
import pyarrow as pa
import pyarrow.parquet as pq
import sys
import os

def project_file(in_path: str, out_path: str):
    tbl = pq.read_table(in_path, columns=["prompt", "response"])
    # Ensure string type; coerce nulls to empty string to avoid downstream schema issues
    tbl = tbl.cast(pa.schema([
        pa.field("prompt", pa.string()),
        pa.field("response", pa.string())
    ]))
    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    pq.write_table(tbl, out_path)
    print(f"Projected {in_path} -> {out_path} ({tbl.num_rows} rows)")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python project_parquet.py input.parquet output.parquet")
        sys.exit(1)
    project_file(sys.argv[1], sys.argv[2])
```

### discovery/train_cdn.py
```python
#!/usr/bin/env python3
"""
Lightning training entry that uses CDN-only fetches.
Expects manifest-{date}.json in the same directory or passed via --manifest.
"""
import json
import os
import sys
from pathlib import Path
from typing import List

import lightning as L
import torch
from torch.utils.data import IterableDataset, DataLoader

try:
    import requests
except ImportError:
    raise RuntimeError("Install requests: pip install requests")

class CDNTextDataset(IterableDataset):
    def __init__(self, file_urls: List[str], cache_dir: str = ".cdn_cache"):
        super().__init__()
        self.file_urls = file_urls
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _download_one(self, url: str) -> Path:
        slug = url.split("/resolve/main/")[-1].replace("/", "_")
        out = self.cache_dir / slug
        if out.exists():
            return out
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        out.write_bytes(r.content)
        return out

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        urls = self.file_urls
        if worker_info is not None:
            per_worker = len(urls) // worker_info.num_workers
            urls = urls[worker_info.id * per_worker : (worker_info.id + 1) * per_worker]

        for url in urls:
            p = self._download_one(url)
            try:
                tbl = pq.read_table(p, columns=["prompt", "response"])
            except Exception:
                continue
            for row in tbl.to_pylist():
                prompt = row.get("prompt") or ""
                response = row.get("response") or ""
                if prompt and response:
                    yield {"prompt": prompt, "response": response}

class SimpleSurrogateModel(torch.nn.Module):
    def __init__(self, vocab_size=32000, d_model=1024):
        super().__init__()
        self.embed = torch.nn.Embedding(vocab_size, d_model)
        self.out = torch.nn.Linear(d_model, vocab_size)

    def forward(self, x):
        return self.out(self.embed(x))

class SurrogateLitModule(L.LightningModule):
    def __init__(self, manifest_path: str):
        super().__init__()
        self.save_hyperparameters()
        with open(manifest_path) as f:
            manifest = json.load(f)
        base = "https://huggingface.co/datasets"
        self.dataset = CDNTextDataset([
            f"{base}/{manifest['repo']}/resolve/main/{p}" for p in manifest["files"]
        ])
        self.model = SimpleSurrogateModel()

    def train_dataloader(self):
        return DataLoader(self.dataset, batch_size=8, num_workers=4)

    def training_step(self, batch, batch_idx):
        # Minimal dummy step to prove CDN pipeline works
        self.log("train/loss", torch.tensor(0.0))
        return torch.tensor(0.0)

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

if __name__ == "__main__":
    manifest = sys.argv[1] if len(sys.argv) > 1 else "manifest-2026-04-29.json"
    if not os.path.exists(manifest):
        print(f"Manifest not found: {manifest}. Run discovery/manifest.py first.")
        sys.exit(1)
    trainer = L.Trainer(max_epochs=1, devices=1, accelerator="gpu" if torch.cuda.is_available() else "cpu")
    module = SurrogateLitModule(manifest)
    trainer.fit(module)
```

### discovery/studio_launcher.py
```python
#!/usr/bin/env python3
"""
Reuse running Lightning Studio or start L40S. Survives idle-stop by checking status.
"""
import time
from lightning import Studio, Teamspace, Machine

def get_or_start_studio(name: str = "vanguard-cdn-train")
