# vanguard / backend

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest: every training run re-enumerates via authenticated HF API → quota burn + 429 risk.
- Recursive/heavy enumeration exposes mixed-schema files and wastes I/O during data discovery.
- Training script still relies on `load_dataset(streaming=True)` on heterogeneous repos → PyArrow CastError at runtime.
- No CDN-only data path: training workers make authenticated API calls during data loading instead of using public CDN URLs.
- Lightning Studio reuse is not enforced: each run risks quota waste by creating new studios instead of reusing running ones.

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/manifest.py` (single, focused) + update `/opt/axentx/vanguard/backend/train.py` to use CDN-only file list and reuse running studios.  
Scope: add manifest persistence (JSON), switch data loader to `hf_hub_download`/CDN URLs, and guard studio creation with reuse logic.

## 3. Implementation

### File: `/opt/axentx/vanguard/backend/manifest.py`
```python
#!/usr/bin/env python3
"""
Build and persist (repo, dateFolder) manifests to avoid HF API enumeration
during training. Uses single non-recursive folder listing + CDN URLs.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from huggingface_hub import list_repo_tree, hf_hub_download


MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)


def _wait_on_429(fn):
    """Simple backoff for HF API 429 (1000 req/5min)."""
    def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 429 or "429" in str(exc):
                    wait = 360
                    print(f"[manifest] HF 429, waiting {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("HF API 429 retries exhausted")
    return wrapped


@_wait_on_429
def build_manifest(repo: str, date_folder: str, out_path: Optional[Path] = None) -> Dict:
    """
    Build manifest for repo/date_folder without recursion.
    Only includes files directly under date_folder (parquet preferred).
    """
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [
        {
            "repo": repo,
            "path": f"{date_folder}/{item.path.split('/')[-1]}",
            "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{item.path.split('/')[-1]}",
            "size": getattr(item, "size", None),
        }
        for item in items
        if item.type == "file" and item.path.lower().endswith((".parquet", ".jsonl"))
    ]

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "note": "CDN-only; do not use authenticated HF API during training data load",
    }

    if out_path is None:
        slug = repo.replace("/", "_")
        out_path = MANIFEST_DIR / f"{slug}__{date_folder}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"[manifest] saved {len(files)} files -> {out_path}")
    return manifest


def load_manifest(repo: str, date_folder: str) -> Optional[Dict]:
    slug = repo.replace("/", "_")
    p = MANIFEST_DIR / f"{slug}__{date_folder}.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def get_or_build_manifest(repo: str, date_folder: str) -> Dict:
    m = load_manifest(repo, date_folder)
    if m is not None:
        print(f"[manifest] reused existing {repo}/{date_folder}")
        return m
    return build_manifest(repo, date_folder)
```

### File: `/opt/axentx/vanguard/backend/train.py` (minimal, surgical diff)
```diff
+ import os
+ import json
+ from pathlib import Path
+ from huggingface_hub import hf_hub_download
+ from lightning.pytorch import seed_everything
+ from lightning.pytorch.utilities import LightningModule
+ from lightning.pytorch import Trainer
+ from lightning.pytorch.strategies import DDPStrategy
+ try:
+     from lightning.pytorch import Studio, Teamspace
+ except Exception:
+     Studio = Teamspace = None
+
+ from .manifest import get_or_build_manifest
+
  # ... existing imports/config ...

+ def _reuse_or_create_studio(name: str, machine: str = "L40S"):
+     """Reuse running studio if exists; otherwise create."""
+     if Studio is None or Teamspace is None:
+         return None
+     for s in Teamspace.studios:
+         if getattr(s, "name", None) == name and getattr(s, "status", None) == "Running":
+             print(f"[studio] reusing running studio: {name}")
+             return s
+     print(f"[studio] creating studio: {name}")
+     return Studio(name=name, machine=machine, create_ok=True)
+
+
+ class CDNParquetDataset:
+     """Lightweight CDN-only dataset: project {prompt,response} at parse time."""
+     def __init__(self, manifest_path_or_dict):
+         if isinstance(manifest_path_or_dict, (str, Path)):
+             with open(manifest_path_or_dict) as f:
+                 self.manifest = json.load(f)
+         else:
+             self.manifest = manifest_path_or_dict
+         self.file_infos = self.manifest.get("files", [])
+
+     def __len__(self):
+         return len(self.file_infos)
+
+     def __getitem__(self, idx):
+         info = self.file_infos[idx]
+         # Download via CDN (no auth header) and project columns at parse time.
+         local_path = hf_hub_download(
+             repo_id=info["repo"],
+             filename=info["path"],
+             repo_type="dataset",
+             # Use CDN URL pattern; hf_hub_download will use CDN when possible.
+         )
+         # Minimal projection: read only required cols to avoid mixed-schema errors.
+         import pyarrow.parquet as pq
+         tbl = pq.read_table(local_path, columns=["prompt", "response"])
+         df = tbl.to_pandas()
+         # Return first row (or implement batching as needed).
+         row = df.iloc[0]
+         return {"prompt": str(row["prompt"]), "response": str(row["response"])}
+
  # In your main training entrypoint:
  def main():
      seed_everything(42, workers=True)

-     # OLD: load_dataset(streaming=True) on heterogeneous repo
-     # dataset = load_dataset("org/surrogate-1", streaming=True)

+     # NEW: manifest + CDN-only
+     repo = "org/surrogate-1"
+     date_folder = "batches/mirror-merged/2026-04-29"
+     manifest = get_or_build_manifest(repo, date_folder)
+     dataset = CDNParquetDataset(manifest)
+
+     # Reuse running Lightning Studio to save quota
+     studio = _reuse_or_create_studio(name="vanguard-surrogate-train", machine="L40S")
+     if studio is not None:
+         # If studio exists and running, attach or run on it.
+         # Example: studio.run() usage depends on your Lightning SDK version.
+         print(f"[studio] studio ready: {studio.name}")
+
      # Continue with Trainer using CDN-backed dataset
      # trainer = Trainer(...strategy=DDPStrategy(), ...)
      # trainer.fit(model, datamodule=...)
```

## 4. Verification

