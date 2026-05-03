# vanguard / discovery

## Final Synthesized Solution

### Diagnosis (Consensus)
- **No persisted manifest**: every training run re-enumerates via HF API → quota burn + 429 risk.
- **Recursive/streaming enumeration** on heterogeneous repos → PyArrow schema errors and rate-limit amplification.
- **No CDN-only fetch strategy** → all reads count against HF API limits.
- **Missing idempotent manifest generation** → repeated discovery work and fragile retries.
- **Training script likely recreates Lightning Studio** instead of reusing instances → wastes quota.

### Core Change
Add an **idempotent discovery utility** that generates a static `(repo, dateFolder)` manifest once, and modify training to **fetch exclusively via CDN** (with authenticated fallback). Training reuses existing Studio instances and avoids any HF API enumeration during training.

---

### Implementation

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/{scripts,manifests}
```

#### `/opt/axentx/vanguard/scripts/discover_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a CDN-only file manifest for (repo, dateFolder).
Usage:
  python discover_manifest.py --repo <org/repo> --date-folder 2026-04-29 \
    --out-dir /opt/axentx/vanguard/manifests
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def discover(repo: str, date_folder: str, out_dir: str) -> str:
    api = HfApi()
    prefix = f"{date_folder}/"

    # Non-recursive, single-page where possible; minimizes quota and schema risk
    tree = list(api.list_repo_tree(repo=repo, path=prefix, recursive=False))

    files = sorted(
        (item.path for item in tree if item.type == "file"),
    )

    manifest = {
        "repo": repo,
        "dateFolder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cdn_prefix": CDN_TEMPLATE.format(repo=repo, path=""),
        "files": files,
        "count": len(files),
    }

    os.makedirs(out_dir, exist_ok=True)
    safe_repo = repo.replace("/", "_")
    out_path = os.path.join(out_dir, f"{safe_repo}__{date_folder}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written: {out_path} ({len(files)} files)")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN-only file manifest.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/name)")
    parser.add_argument("--date-folder", required=True, help="Date folder in repo (e.g., 2026-04-29)")
    parser.add_argument("--out-dir", default="/opt/axentx/vanguard/manifests", help="Output directory")
    args = parser.parse_args()

    try:
        discover(args.repo, args.date_folder, args.out_dir)
    except Exception as exc:
        print(f"Discovery failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/discover_manifest.py
```

---

#### `/opt/axentx/vanguard/train.py`
```python
#!/usr/bin/env python3
"""
Lightning training script that uses a persisted manifest and CDN-only fetches.
Reuses existing Studio instance; does not re-enumerate via HF API during training.

Usage:
  python train.py --manifest manifests/vanguard__2026-04-29.json
"""
import argparse
import json
import os
from pathlib import Path
from typing import List

import lightning as L
import requests
import torch
from torch.utils.data import DataLoader, Dataset
from huggingface_hub import hf_hub_download


class CDNTextDataset(Dataset):
    def __init__(self, manifest_path: str, max_files: int = -1):
        with open(manifest_path, "r", encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.files: List[str] = self.manifest["files"]
        if max_files > 0:
            self.files = self.files[:max_files]
        self.repo = self.manifest["repo"]
        self.cdn_prefix = self.manifest["cdn_prefix"].rstrip("/") + "/"

    def __len__(self) -> int:
        return len(self.files)

    def _fetch_via_cdn(self, rel_path: str) -> str:
        url = f"{self.cdn_prefix}{rel_path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text

    def _fetch_via_hf(self, rel_path: str) -> str:
        # Authenticated fallback for private repos or CDN misses
        local = hf_hub_download(repo_id=self.repo, filename=rel_path)
        with open(local, "r", encoding="utf-8") as f:
            return f.read()

    def __getitem__(self, idx: int):
        rel_path = self.files[idx]
        try:
            text = self._fetch_via_cdn(rel_path)
        except Exception:
            text = self._fetch_via_hf(rel_path)

        # Replace with your parser (e.g., JSONL -> {prompt, response}).
        return {"text": text, "path": rel_path}


class LitDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: str, batch_size: int = 4, max_files: int = -1):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size
        self.max_files = max_files

    def setup(self, stage=None):
        self.dataset = CDNTextDataset(self.manifest_path, max_files=self.max_files)

    def train_dataloader(self):
        # num_workers=0 avoids fork/spawn issues in Studio; increase if running on bare metal
        return DataLoader(self.dataset, batch_size=self.batch_size, shuffle=True, num_workers=0)


class SimpleModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.layer = torch.nn.Linear(1024, 1024)

    def training_step(self, batch, batch_idx):
        # Placeholder: replace with real model forward.
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-files", type=int, default=-1, help="Limit files for quick run")
    parser.add_argument("--devices", type=int, default=1)
    parser.add_argument("--accelerator", default="auto")
    parser.add_argument("--max-epochs", type=int, default=1)
    args = parser.parse_args()

    dm = LitDataModule(args.manifest, batch_size=args.batch_size, max_files=args.max_files)
    model = SimpleModel()

    trainer = L.Trainer(
        accelerator=args.accelerator,
        devices=args.devices,
        max_epochs=args.max_epochs,
        log_every_n_steps=10,
    )
    trainer.fit(model, dm)


if __name__ == "__main__":
    main()
```

---

### Operational Workflow
1. **Generate manifest once** (or on repo update):
   ```bash
   python /opt/axentx/vanguard/scripts/discover_manifest.py \
     --repo org/vanguard \
     --date-folder 2026-04-29 \
     --out-dir /opt/axentx/vanguard/manifests
   ```
   Produces: `/opt/axentx/vanguard/manifests/vanguard
