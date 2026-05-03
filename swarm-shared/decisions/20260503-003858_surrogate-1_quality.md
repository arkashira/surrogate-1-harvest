# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs and a training script that uses **CDN-only** fetches (zero HF API calls during training). This eliminates 429s and quota pressure.

### Steps
1. Create `tools/snapshot_manifest.py` — single `list_repo_tree` call for one date folder, outputs `file_manifest.json` with CDN URLs and shard metadata.
2. Create `tools/train_cdn.py` — Lightning training script that reads `file_manifest.json`, downloads via CDN (`/resolve/main/...`), projects to `{prompt, response}`, streams into DataLoader.
3. Add lightweight reuse guard for Lightning Studio (list before create) and idle-stop restart logic.
4. Update `requirements.txt` if needed (add `lightning`, keep `datasets` for optional fallback).
5. Quick smoke test: run snapshot locally, verify manifest, run one training step.

---

### 1) tools/snapshot_manifest.py

```python
#!/usr/bin/env python3
"""
Generate file_manifest.json for one date partition.
Usage:
  python tools/snapshot_manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out file_manifest.json
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, out_path: str):
    api = HfApi()
    folder = f"{date}"  # or "batches/public-merged/{date}" depending on layout
    try:
        items = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    except Exception as e:
        # fallback: try common prefix
        try:
            items = api.list_repo_tree(repo=repo, path=f"batches/public-merged/{date}", recursive=False)
            folder = f"batches/public-merged/{date}"
        except Exception as e2:
            print(f"Failed to list {repo} at {date}: {e2}", file=sys.stderr)
            sys.exit(1)

    files = []
    for item in items:
        if not item.rfilename.lower().endswith((".parquet", ".jsonl", ".json")):
            continue
        path = item.rfilename
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=path)
        slug = Path(path).stem
        files.append({
            "repo": repo,
            "path": path,
            "cdn_url": cdn_url,
            "filename": Path(path).name,
            "slug": slug,
            "size": getattr(item, "size", None),
            "lfs": getattr(item, "lfs", None)
        })

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "repo": repo,
        "date": date,
        "folder": folder,
        "count": len(files),
        "files": files
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote manifest with {len(files)} files -> {out}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snapshot HF dataset partition manifest")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date partition (YYYY-MM-DD)")
    parser.add_argument("--out", default="file_manifest.json")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

Make executable:
```bash
chmod +x tools/snapshot_manifest.py
```

---

### 2) tools/train_cdn.py

```python
#!/usr/bin/env python3
"""
Lightning training using CDN-only fetches (zero HF API calls during data load).
Reads file_manifest.json and streams parquet/jsonl via CDN URLs.
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterator, List

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader
import lightning as L

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "file_manifest.json")

class CDNIterableDataset(IterableDataset):
    def __init__(self, files: List[Dict], max_files: int = None):
        super().__init__()
        self.files = files[:max_files] if max_files else files

    def _stream_file(self, entry: Dict) -> Iterator[Dict]:
        url = entry["cdn_url"]
        path = entry["path"]
        try:
            resp = requests.get(url, stream=True, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"Failed to fetch {url}: {e}", file=sys.stderr)
            return

        local_path = f"/tmp/{hash(url) % 100000}.parquet"
        try:
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            table = pq.read_table(local_path)
            # Project to {prompt, response} only at parse time
            if "prompt" in table.column_names and "response" in table.column_names:
                for i in range(table.num_rows):
                    yield {
                        "prompt": table["prompt"][i].as_py(),
                        "response": table["response"][i].as_py(),
                        "_source_file": path
                    }
            else:
                # fallback: yield raw rows
                for i in range(table.num_rows):
                    yield {k: table[k][i].as_py() for k in table.column_names}
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            files = self.files
        else:
            per_worker = len(self.files) // worker_info.num_workers
            files = self.files[worker_info.id * per_worker : (worker_info.id + 1) * per_worker]

        for entry in files:
            yield from self._stream_file(entry)

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: str, batch_size: int = 8, max_files: int = None):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size
        self.max_files = max_files

    def setup(self, stage=None):
        with open(self.manifest_path) as f:
            manifest = json.load(f)
        self.dataset = CDNIterableDataset(manifest["files"], max_files=self.max_files)

    def train_dataloader(self):
        return DataLoader(self.dataset, batch_size=self.batch_size, num_workers=2)

class SurrogateModel(L.LightningModule):
    def __init__(self, lr: float = 1e-4):
        super().__init__()
        self.lr = lr
        # Minimal model for smoke test; replace with your actual model
        self.net = torch.nn.Linear(1024, 1024)

    def training_step(self, batch, batch_idx):
        # Placeholder: adapt to your actual model/tokenizer
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=self.lr)

def run_training(manifest_path: str = MANIFEST_PATH, max_files: int = 4, limit_train_batches: int = 2):
    # Reuse running studio if available
    try:
        from lightning.pytorch import Studio, Teamspace
        studios = Teamspace.studios()
        running = [s for s in studios if s.name == "surrogate-1-train" and s.status == "Running"]
        if running:
            studio = running[0]
