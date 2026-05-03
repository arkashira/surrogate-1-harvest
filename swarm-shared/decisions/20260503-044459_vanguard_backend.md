# vanguard / backend

## 1. Diagnosis

- No content-addressed manifest per date folder forces runtime repo enumeration via `list_repo_tree`/`load_dataset`, triggering HF API 429s and non-reproducible epochs.
- Training scripts likely call HF API during data loading (no pre-listed file manifest), wasting quota and risking mid-epoch failures.
- Missing deterministic `{path, sha256}` snapshot means training runs can diverge across retries (files added/removed between runs).
- No CDN-only data path: training still uses authenticated `/api/` endpoints instead of public CDN URLs, hitting stricter rate limits.
- No lightweight orchestration wrapper on the Mac to produce the manifest once and embed it in the Lightning training job.

## 2. Proposed change

Create a single backend orchestration script that:
- Runs on the Mac (or any dev machine) after the rate-limit window clears.
- Lists one date folder via HF API once (`list_repo_tree` non-recursive).
- Produces `manifests/{date}/files.json` with `{path, sha256, size, url}` (CDN URLs).
- Optionally produces a small `train.py` patch or env file that points Lightning training at the manifest for CDN-only fetches.

File scope:
- Add `/opt/axentx/vanguard/scripts/build_manifest.py`
- Add `/opt/axentx/vanguard/training/train.py` (or patch existing) to accept `MANIFEST_PATH` and use CDN URLs via `datasets` or custom `IterableDataset`.

## 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/{scripts,training,manifests}
```

`/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Build a content-addressed manifest for one date folder in a HF dataset repo.
Usage:
  HF_REPO="datasets/username/repo" \
  DATE_FOLDER="2026-04-29" \
  python build_manifest.py --out manifests/2026-04-29/files.json
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_path: Path):
    """
    List top-level files in repo/date_folder/ and emit manifest.
    Non-recursive by design; nested folders can be handled by repeating per subfolder.
    """
    prefix = f"{date_folder}/"
    entries = list_repo_tree(repo, path=date_folder, recursive=False)

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        path = f"{date_folder}/{entry.path}"
        url = CDN_TEMPLATE.format(repo=repo, path=path)
        # Note: sha256 requires download; for speed we use entry.lfs if available,
        # otherwise placeholder. For surrogate-1 we only need path+url for CDN fetch.
        files.append({
            "path": path,
            "url": url,
            "size": getattr(entry, "size", None),
            "sha256": getattr(entry, "lfs", {}).get("oid", None) if hasattr(entry, "lfs") else None,
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files -> {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF CDN manifest for a date folder.")
    parser.add_argument("--repo", default=os.getenv("HF_REPO"), help="HF dataset repo (e.g., datasets/username/repo)")
    parser.add_argument("--date", default=os.getenv("DATE_FOLDER"), help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out", type=Path, required=True, help="Output JSON path")
    args = parser.parse_args()

    if not args.repo or not args.date:
        parser.error("Provide --repo and --date or env HF_REPO + DATE_FOLDER")

    build_manifest(args.repo, args.date, args.out)
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

`/opt/axentx/vanguard/training/train.py` (minimal CDN-only dataset loader)
```python
#!/usr/bin/env python3
"""
Lightning-compatible training entrypoint that uses CDN URLs from a manifest.
Keeps HF API calls to zero during training.
"""

import json
import os
from pathlib import Path
from typing import Iterator

import torch
from torch.utils.data import IterableDataset, DataLoader
from lightning import LightningModule, Trainer

try:
    import datasets
except ImportError:
    datasets = None

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifests/2026-04-29/files.json")

class CDNTextDataset(IterableDataset):
    """
    Stream text pairs from manifest files via CDN URLs.
    Assumes files are line-delimited JSON with {prompt, response} or similar.
    """
    def __init__(self, manifest_path: str):
        super().__init__()
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.urls = [f["url"] for f in self.manifest["files"]]

    def __iter__(self) -> Iterator[dict]:
        worker_info = torch.utils.data.get_worker_info()
        urls = self.urls
        if worker_info is not None:
            # shard per worker
            per_worker = len(urls) // worker_info.num_workers
            urls = urls[worker_info.id * per_worker : (worker_info.id + 1) * per_worker]

        for url in urls:
            try:
                import requests
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                # Customize parsing per your file format.
                # Example: assume each line is {"prompt": "...", "response": "..."}
                for line in resp.text.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        yield item
                    except json.JSONDecodeError:
                        continue
            except Exception as exc:
                print(f"Failed to fetch {url}: {exc}")
                continue

class Surrogate1Module(LightningModule):
    def __init__(self):
        super().__init__()
        self.model = torch.nn.Linear(10, 1)  # placeholder

    def training_step(self, batch, batch_idx):
        # Replace with real forward + loss
        loss = torch.tensor(0.0)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)

    def train_dataloader(self):
        dataset = CDNTextDataset(MANIFEST_PATH)
        return DataLoader(dataset, batch_size=8, num_workers=2)

if __name__ == "__main__":
    # Local test run
    model = Surrogate1Module()
    trainer = Trainer(max_epochs=1, limit_train_batches=2)
    trainer.fit(model)
```

Optional: small launcher for Lightning Studio reuse (Mac orchestration only)
`/opt/axentx/vanguard/scripts/launch_studio.py`
```python
#!/usr/bin/env python3
"""
Reuse a running Lightning Studio if present; otherwise start one.
Keeps quota usage low.
"""

import os
from lightning import Studio, Teamspace, Machine

def get_or_start_studio(name: str = "vanguard-l40s"):
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return
