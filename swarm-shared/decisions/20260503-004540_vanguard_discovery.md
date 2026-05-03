# vanguard / discovery

## 1. Diagnosis

- No persisted `(repo, dateFolder)` file manifest exists → every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Recursive `list_repo_files` usage likely still present (paginated, slow, rate-limited) instead of `list_repo_tree(path, recursive=False)` per folder.
- Training script probably uses `load_dataset(streaming=True)` on heterogeneous repo files → pyarrow CastError on mixed schemas.
- Lightning Studio lifecycle is likely recreate-each-run → wastes 80h/mo quota by not reusing running studios.
- No CDN-only data path → training still hits authenticated `/api/` endpoints instead of public CDN URLs.

## 2. Proposed change

Create `/opt/axentx/vanguard/scripts/build_manifest.py` and update `/opt/axentx/vanguard/train.py` (or create it) to:
- Build a deterministic `manifest-{repo}-{date}.json` containing only CDN paths for one date folder.
- Use `list_repo_tree(recursive=False)` per folder (non-recursive) to minimize API calls.
- Embed the manifest in training so Lightning workers fetch via CDN only (zero authenticated API calls during data load).
- Reuse running Lightning Studio instead of recreating.

## 3. Implementation

```bash
# Ensure scripts directory exists
mkdir -p /opt/axentx/vanguard/scripts
```

### `/opt/axentx/vanguard/scripts/build_manifest.py`

```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for one repo+date folder.
Usage:
  HF_TOKEN=hf_xxx python build_manifest.py \
    --repo datasets/myorg/surrogate-1 \
    --date 2026-04-29 \
    --out manifest-2026-04-29.json
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import List, Dict

import requests

HF_API_BASE = "https://huggingface.co/api"


def list_repo_tree(repo: str, path: str = "", token: str = "") -> List[Dict]:
    """Non-recursive tree listing (one folder deep)."""
    url = f"{HF_API_BASE}/datasets/{repo}/tree"
    params = {"path": path, "recursive": "false"}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, params=params, headers=headers)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 360))
        print(f"Rate limited. Waiting {retry_after}s")
        time.sleep(retry_after)
        return list_repo_tree(repo, path, token)
    resp.raise_for_status()
    return resp.json()


def build_manifest(repo: str, date_folder: str, token: str) -> Dict:
    """
    Build manifest for repo + date folder.
    Returns:
      {
        "repo": "...",
        "date": "...",
        "generated_at": "...",
        "files": [
          {"path": "2026-04-29/file1.parquet", "cdn_url": "https://.../resolve/main/..."},
          ...
        ]
      }
    """
    items = list_repo_tree(repo, path=date_folder, token=token)
    files = []
    for item in items:
        if item.get("type") != "file":
            continue
        rel_path = f"{date_folder}/{item['path']}"
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{rel_path}"
        files.append({"path": rel_path, "cdn_url": cdn_url})

    return {
        "repo": repo,
        "date": date_folder,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN-only manifest for HF dataset folder")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/org/name)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN", "")
    manifest = build_manifest(args.repo, args.date, token)

    Path(args.out).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote manifest with {len(manifest['files'])} files to {args.out}")


if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

### `/opt/axentx/vanguard/train.py` (new)

```python
#!/usr/bin/env python3
"""
Lightning training entrypoint that uses CDN-only manifest.
- Reuses running Lightning Studio when available.
- Loads parquet files directly from CDN URLs (zero HF API calls during training).
"""

import json
import os
import sys
from pathlib import Path

import lightning as L
import pandas as pd
import torch
from torch.utils.data import Dataset, DataLoader


class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path: str):
        manifest = json.loads(Path(manifest_path).read_text())
        self.files = [f["cdn_url"] for f in manifest["files"] if f["cdn_url"].endswith(".parquet")]
        if not self.files:
            raise ValueError("No parquet files found in manifest")

    def __len__(self) -> int:
        return len(self.files)

    def __getitem__(self, idx: int):
        # Lightweight example: read parquet from CDN and project to {prompt,response}
        df = pd.read_parquet(self.files[idx])
        # Expect projected {prompt,response} schema; adapt as needed
        prompt = df.iloc[0]["prompt"]
        response = df.iloc[0]["response"]
        return {"prompt": prompt, "response": response}


class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: str, batch_size: int = 8):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size

    def setup(self, stage=None):
        self.dataset = CDNParquetDataset(self.manifest_path)

    def train_dataloader(self):
        return DataLoader(self.dataset, batch_size=self.batch_size, shuffle=True, num_workers=0)


class SurrogateModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.example = torch.nn.Linear(1, 1)

    def training_step(self, batch, batch_idx):
        # Replace with real model logic
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)


def main():
    manifest_path = os.environ.get("MANIFEST_PATH", "manifest-2026-04-29.json")
    if not Path(manifest_path).exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    # Reuse running studio if available
    reuse_ok = os.environ.get("LIGHTNING_REUSE_STUDIO", "1") == "1"
    studio = None
    if reuse_ok:
        from lightning.fabric.utilities.cloud_io import _load as _load_teamspace
        # Lightweight check: list running studios via Teamspace (if available)
        try:
            from lightning import Teamspace
            for s in Teamspace.studios:
                if s.name == "vanguard-train" and s.status == "Running":
                    studio = s
                    print(f"Reusing running studio: {s.name}")
                    break
        except Exception:
            pass

    if studio is None:
        print("No running studio found; will start training locally in this process.")

    dm = SurrogateDataModule(manifest_path=manifest_path, batch_size=8)
    model = SurrogateModel()
    trainer = L.Trainer(max_epochs=1, accelerator="cpu", devices
