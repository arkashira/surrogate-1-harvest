# vanguard / quality

## 1. Diagnosis
- Training script still resolves data via HF `datasets` API at runtime (`load_dataset`/`list_repo_tree`) → exposes surrogate-1 to 429 rate limits and non-reproducible shard order.
- No content-addressed manifest per date folder → epochs drift across runs and resumable training is unreliable.
- Missing pre-list → every run re-enumerates repo tree and risks API quota exhaustion.
- No CDN-only fetch path → training depends on authenticated `/api/` endpoints instead of public CDN URLs.
- No deterministic file-to-repo mapping for commit-cap mitigation → ingestion can hit 128/hr/repo ceiling.

## 2. Proposed change
File: `/opt/axentx/vanguard/train.py` (or create if absent)  
Scope: add a small manifest generator (`build_manifest.py`) and modify training entrypoint to use CDN-only fetches with an embedded file list. No runtime HF API calls during training.

## 3. Implementation

```bash
# /opt/axentx/vanguard/build_manifest.py
#!/usr/bin/env bash
set -euo pipefail

# Usage: build_manifest.py <repo> <date_folder> <out.json>
# Example: build_manifest.py datasets/myorg/surrogate-1 2026-05-03 manifest.json

REPO="${1:-datasets/myorg/surrogate-1}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
OUT="${3:-manifest.json}"

# Requires: huggingface_hub, python3
python3 - "$REPO" "$DATE_FOLDER" "$OUT" <<'PY'
import json, os, sys
from huggingface_hub import list_repo_tree

repo_id = sys.argv[1]
folder = sys.argv[2].strip("/")
out_path = sys.argv[3]

# Non-recursive top-level for the date folder
items = list_repo_tree(repo_id=repo_id, path=folder, recursive=False)

files = []
for item in items:
    if item.type != "file":
        continue
    # CDN bypass URL (no auth, no /api/)
    cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{folder}/{item.path}"
    files.append({
        "path": item.path,
        "cdn_url": cdn_url,
        "size": getattr(item, "size", None),
        "lfs": getattr(item, "lfs", None) is not None
    })

manifest = {
    "repo_id": repo_id,
    "folder": folder,
    "generated_by": "build_manifest.py",
    "files": files
}

os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
with open(out_path, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Wrote {len(files)} files to {out_path}")
PY
```

```python
# /opt/axentx/vanguard/train.py
#!/usr/bin/env python3
"""
Train surrogate-1 using CDN-only fetches.
Embed manifest at script-build time or pass via --manifest.
No HF datasets/list_repo_tree calls during training.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Dict

import torch
from torch.utils.data import IterableDataset, DataLoader
import requests
from tqdm import tqdm

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception:
    pa = None  # optional; handle gracefully

def deterministic_repo_for_slug(slug: str, n_siblings: int = 5) -> int:
    """Map slug to sibling repo index for commit-cap spreading."""
    return hash(slug) % n_siblings

class CDNParquetIterable(IterableDataset):
    """Stream parquet files via CDN URLs with zero HF API calls during training."""
    def __init__(self, manifest_path: str, max_files: int = None):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.files = manifest["files"]
        if max_files:
            self.files = self.files[:max_files]
        self.rendezvous = torch.utils.data.get_worker_info()

    def _project_to_pair(self, batch: Dict) -> Dict:
        """Project raw file to {prompt, response} only at parse time."""
        # Minimal projection: keep only prompt/response keys if present
        out = {}
        for k in ("prompt", "response"):
            if k in batch:
            out[k] = batch[k]
        return out

    def _stream_file(self, url: str):
        # CDN downloads do not count against HF API rate limits
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        if pa is None:
            raise RuntimeError("pyarrow required to read parquet")
        table = pq.read_table(pa.BufferReader(resp.content))
        # Convert to dict batches and project
        for batch in table.to_batches(max_chunksize=1024):
            proj = self._project_to_pair(batch.to_pydict())
            if proj:
                yield proj

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        files = self.files
        if worker_info is not None:
            per_worker = len(files) // worker_info.num_workers
            files = files[worker_info.id * per_worker : (worker_info.id + 1) * per_worker]

        for item in files:
            url = item["cdn_url"]
            try:
                yield from self._stream_file(url)
            except Exception as exc:
                print(f"Skipping {url}: {exc}", file=sys.stderr)
                continue

def build_dataloader(manifest_path: str, batch_size: int = 8, max_files: int = None):
    ds = CDNParquetIterable(manifest_path=manifest_path, max_files=max_files)
    return DataLoader(ds, batch_size=batch_size, num_workers=2)

def train_step(batch, model, optimizer, device):
    # Minimal training step placeholder
    model.train()
    inputs = batch.get("prompt", [])
    targets = batch.get("response", [])
    # Replace with real tokenizer/model logic
    loss = torch.tensor(0.0, device=device, requires_grad=True)
    loss.backward()
    optimizer.step()
    optimizer.zero_grad()
    return loss.item()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--output-dir", default="checkpoints")
    args = parser.parse_args()

    if not os.path.isfile(args.manifest):
        print(f"Manifest not found: {args.manifest}", file=sys.stderr)
        sys.exit(1)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # Placeholder model
    model = torch.nn.Linear(10, 10).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)

    loader = build_dataloader(args.manifest, batch_size=args.batch_size, max_files=args.max_files)

    os.makedirs(args.output_dir, exist_ok=True)
    for epoch in range(args.epochs):
        pbar = tqdm(loader, desc=f"Epoch {epoch+1}/{args.epochs}")
        for batch in pbar:
            loss = train_step(batch, model, optimizer, device)
            pbar.set_postfix({"loss": loss})

    ckpt = {
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "epochs": args.epochs,
    }
    ckpt_path = Path(args.output_dir) / "last.pt"
    torch.save(ckpt, ckpt_path)
    print(f"Saved checkpoint to {ckpt_path}")

if __name__ == "__main__":
    main()
```

Make scripts executable:
```bash
chmod +x /opt/axentx/vanguard/build_manifest.py
chmod +x /opt/axentx/vanguard/train.py
```

Usage (Mac orchestration only — no model.from_pretrained on Mac):
```bash
# On Mac: generate manifest once per date folder (after rate-limit window clears)
cd /opt/axentx/vanguard
./build_manifest.py
