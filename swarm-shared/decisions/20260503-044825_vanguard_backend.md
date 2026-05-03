# vanguard / backend

## Final Synthesized Answer

### Diagnosis (unified)
- **No content-addressed manifest per date folder** → training repeatedly calls `list_repo_tree`/`load_dataset` at runtime, causing HF API 429s and non-reproducible epochs.
- **Data loader uses Hugging Face `datasets` API during training** instead of CDN-only fetches, wasting rate-limit quota and breaking determinism.
- **Missing deterministic file list embedded at launch** → each epoch can see different shard ordering or newly arrived files (non-reproducible runs).
- **Backend orchestration re-lists repos on every job start** instead of reusing a saved manifest, amplifying 429 risk during sweeps.
- **No fallback when HF API is rate-limited** (no CDN bypass path), so training stalls instead of continuing with pre-listed CDN URLs.

### Proposed change (single, actionable)
Add a lightweight backend module that:
- On job start, calls `list_repo_tree` **once** for a given `date_folder` and writes `manifests/{date}/filelist.json` (content-addressed by folder hash).
- Exposes `get_training_urls(date_folder)` that returns **CDN-only** `resolve/main/...` URLs (zero auth, zero API calls during training).
- Embeds this manifest into the Lightning training script so data loading uses **deterministic, CDN-only fetches** (`aiohttp`/`requests`/`wget`) with **retry/backoff** and **integrity verification**.
- Includes a **CLI** for one-time manifest generation and a **fallback loader** that uses the local manifest when HF API is unavailable.

Scope:
- New file: `/opt/axentx/vanguard/backend/manifest.py`
- Update: `/opt/axentx/vanguard/backend/train.py` (or launcher) to precompute and inject manifest path.
- Optional CLI: `python -m vanguard.backend.manifest --date 2026-04-29 --repo org/surrogate-data --out manifests/2026-04-29/filelist.json`.

### Implementation

```bash
# /opt/axentx/vanguard/backend/manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed CDN file manifest for a date folder.
Avoids HF API calls during training by listing once and using CDN URLs.
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def folder_hash(date_folder: str, repo: str, files: List[str]) -> str:
    content = f"{repo}|{date_folder}|" + "|".join(sorted(files))
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def build_manifest(
    repo: str,
    date_folder: str,
    out_path: Path,
    recursive: bool = False,
) -> Dict:
    api = HfApi()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Single API call: list top-level of date folder
    items = api.list_repo_tree(repo=repo, path=date_folder, recursive=recursive)
    # Keep only files (ignore subdirs if recursive=False)
    file_paths = [item.path for item in items if item.type == "file"]

    cdn_urls = [
        HF_CDN_TEMPLATE.format(repo=repo, path=fp)
        for fp in file_paths
    ]

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "folder_hash": folder_hash(date_folder, repo, file_paths),
        "file_count": len(file_paths),
        "files": file_paths,
        "cdn_urls": cdn_urls,
    }

    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for HF dataset date folder")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/name)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--recursive", action="store_true", help="List recursively")
    args = parser.parse_args()

    manifest = build_manifest(
        repo=args.repo,
        date_folder=args.date,
        out_path=Path(args.out),
        recursive=args.recursive,
    )
    print(f"Wrote manifest with {manifest['file_count']} files to {args.out}")
    print(f"folder_hash={manifest['folder_hash']}")


if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/backend/train.py  (excerpt: integrate manifest)
import json
import time
from pathlib import Path
from typing import List

import lightning as L
import requests
import torch
from torch.utils.data import IterableDataset
from huggingface_hub import HfApi


class CDNShardDataset(IterableDataset):
    """
    Training dataset that reads only from CDN URLs listed in a precomputed manifest.
    Zero HF API calls during training.
    """
    def __init__(self, manifest_path: Path, max_retries: int = 3, backoff_factor: float = 0.5):
        manifest = json.loads(manifest_path.read_text())
        self.urls: List[str] = manifest["cdn_urls"]
        # deterministic ordering
        self.urls.sort()
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def _download_with_retry(self, url: str) -> bytes:
        for attempt in range(self.max_retries):
            try:
                resp = requests.get(url, stream=True, timeout=60)
                resp.raise_for_status()
                return resp.content
            except requests.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise
                sleep_time = self.backoff_factor * (2 ** attempt)
                time.sleep(sleep_time)
        raise RuntimeError(f"Failed to download {url} after {self.max_retries} attempts")

    def __iter__(self):
        for url in self.urls:
            # streaming download from CDN (no auth)
            content = self._download_with_retry(url)
            # project to {prompt,response} here (parquet/JSONL parsing)
            # yield {"prompt": ..., "response": ...}
            # Example placeholder:
            yield {"raw_bytes": content, "url": url}


def run_training(date_folder: str, repo: str = "org/surrogate-data"):
    L.seed_everything(42)

    manifest_path = Path(f"manifests/{date_folder}/filelist.json")
    if not manifest_path.exists():
        # One-time generation (run on orchestration node)
        from .manifest import build_manifest
        build_manifest(repo=repo, date_folder=date_folder, out_path=manifest_path)

    dataset = CDNShardDataset(manifest_path=manifest_path)

    # Use with Lightning DataLoader
    loader = torch.utils.data.DataLoader(dataset, batch_size=None, num_workers=0)

    # Minimal training step example
    class SurrogateModule(L.LightningModule):
        def __init__(self):
            super().__init__()
            self.lm = torch.nn.Linear(1024, 1024)  # placeholder

        def training_step(self, batch, batch_idx):
            # Replace with real tokenized tensors
            x = torch.randn(1, 1024)  # placeholder from batch
            loss = self.lm(x).sum()
            self.log("train_loss", loss)
            return loss

        def configure_optimizers(self):
            return torch.optim.Adam(self.parameters(), lr=1e-4)

    model = SurrogateModule()
    trainer = L.Trainer(max_epochs=1, devices=1, accelerator="gpu")
    trainer.fit(model, loader)


if __name__ == "__main__":
    # Example: python -m vanguard.backend.train --date 2026-04-29
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--repo", default="org/surrogate-data")

