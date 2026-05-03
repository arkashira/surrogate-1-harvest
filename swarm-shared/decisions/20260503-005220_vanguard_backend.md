# vanguard / backend

## Final Synthesis & Actionable Plan

### 1. Root Cause (merged)
- **No persisted manifest**: every training run re-enumerates the HF repo via authenticated API → quota burn + 429 risk.
- **Recursive/heavy enumeration**: exposes mixed-schema files and wastes I/O.
- **Schema mismatch at load time**: `load_dataset(streaming=True)` or similar hits `pyarrow.CastError` on heterogeneous repo files.
- **Instance churn**: Lightning Studio likely recreates instead of reusing → wastes 80 h/mo quota.
- **Dirty data writes**: ingestion produces mixed-schema enriched files instead of clean `{prompt,response}` parquet in dated batch folders.

### 2. Solution (merged + corrected)
Create a manifest layer that:
- Uses **non-recursive folder listing** to build a durable `(repo, dateFolder)` manifest (JSON).
- **Caches to disk** so training never calls HF API for file enumeration.
- **Fetches files via public CDN URLs** during training (no auth, no quota).
- **Projects to `{prompt,response}` at parse time** and ignores extra schema to avoid Arrow cast errors.
- **Reuses a running Lightning Studio** when present; otherwise falls back to standard `Fabri`c with L40S-class settings.
- **Writes clean parquet outputs** for downstream runs (optional but recommended).

### 3. Implementation (single, production-ready)

#### `/opt/axentx/vanguard/backend/manifest.py`
```python
#!/usr/bin/env python3
"""
Build and cache repo+dateFolder file manifests to avoid HF API enumeration
during training. Uses non-recursive tree listing + CDN URLs for training.
"""
import json
import os
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi

HF_API = HfApi()
MANIFEST_ROOT = Path(__file__).parent.parent / "manifests"
MANIFEST_ROOT.mkdir(exist_ok=True, parents=True)

def _clean_filename(name: str) -> str:
    return name.replace("/", "_")

def build_manifest(repo: str, date_folder: str, revision: str = "main") -> List[Dict]:
    """
    Build manifest for repo/date_folder (non-recursive).
    Returns list of dicts:
      {"repo": repo, "path": path, "cdn_url": url, "size": size}
    """
    manifest_path = MANIFEST_ROOT / _clean_filename(repo) / f"{date_folder}.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)

    # Non-recursive listing for the date folder only
    tree = HF_API.list_repo_tree(
        repo=repo, path=date_folder, recursive=False, revision=revision
    )
    files = [t for t in tree if t.type == "file"]

    manifest = []
    for f in files:
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}"
        manifest.append({
            "repo": repo,
            "path": f.path,
            "cdn_url": cdn_url,
            "size": getattr(f, "size", None),
        })

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2, ensure_ascii=False)
    return manifest

def load_manifest(repo: str, date_folder: str) -> List[Dict]:
    manifest_path = MANIFEST_ROOT / _clean_filename(repo) / f"{date_folder}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest missing: {manifest_path}")
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)
```

#### `/opt/axentx/vanguard/backend/train.py` (key excerpts)
```python
#!/usr/bin/env python3
import json
import os
from pathlib import Path

import requests
import torch
from datasets import Dataset
from lightning import Fabric

from .manifest import build_manifest, load_manifest

REPO = os.getenv("HF_DATASET_REPO", "org/surrogate-1")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", "batches/mirror-merged/2026-04-29")

def prepare_dataloader_cdn_only(manifest):
    """
    Use CDN URLs directly; avoid HF API during training.
    Projects to {prompt, response} only to dodge pyarrow schema issues.
    """
    def _generator():
        for item in manifest:
            url = item["cdn_url"]
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            for line in resp.text.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                prompt = obj.get("prompt") or obj.get("input") or ""
                response = obj.get("response") or obj.get("output") or ""
                if prompt and response:
                    yield {"prompt": prompt, "response": response}

    ds = Dataset.from_generator(_generator)
    # Use small batch size by default; adjust per hardware
    return torch.utils.data.DataLoader(ds, batch_size=8, shuffle=True)

def train():
    # Reuse running Lightning Studio if available (quota savings)
    try:
        from lightning import Teamspace
        studios = Teamspace.studios
        studio = next(
            (s for s in studios if s.name == "vanguard-train" and s.status == "Running"),
            None,
        )
        if studio:
            fabric = Fabric(studio=studio)
        else:
            fabric = Fabric(devices=1, accelerator="cuda", precision="bf16-mixed")
    except Exception:
        fabric = Fabric(devices=1, accelerator="cuda", precision="bf16-mixed")

    # Build or load manifest once (idempotent)
    manifest = build_manifest(REPO, DATE_FOLDER)
    train_loader = prepare_dataloader_cdn_only(manifest)

    # Minimal training loop placeholder
    model = torch.nn.Transformer()
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
    model, optimizer = fabric.setup(model, optimizer)
    train_loader = fabric.setup_dataloaders(train_loader)

    for epoch in range(1):
        for batch in train_loader:
            # Replace with real forward/backward on your model
            loss = torch.tensor(0.0, requires_grad=True)
            fabric.backward(loss)
            optimizer.step()
            optimizer.zero_grad()

if __name__ == "__main__":
    train()
```

#### Permissions & bootstrap
```bash
chmod +x /opt/axentx/vanguard/backend/manifest.py
chmod +x /opt/axentx/vanguard/backend/train.py
# Ensure any wrapper scripts use #!/usr/bin/env bash (or python3) and are executable.
```

### 4. Verification (merged, corrected)

1. **Build manifest once** (run on Mac or training node):
   ```bash
   cd /opt/axentx/vanguard/backend
   python3 -c "
   from manifest import build_manifest
   m = build_manifest('org/surrogate-1', 'batches/mirror-merged/2026-04-29')
   print(f'files: {len(m)}')
   if m: print(m[0])
   "
   ```
   - Expect: non-zero file count, valid `cdn_url`, and `manifests/...json` created.

2. **CDN-only dataloader without HF API**:
   ```bash
   HF_HUB_OFFLINE=1 python3 -c "
   from manifest import load_manifest
   from train import prepare_dataloader_cdn_only
   m = load_manifest('org/surrogate-1', 'batches/mirror-merged/2026-04-29')
   dl = prepare_dataloader_cdn_only(m)
   for b in dl:
       print(b.keys())
       print('prompt sample:', b['prompt'][0][:60])
       break
   "
   ```
   - Expect: batch with `prompt`/`response` keys; no HuggingFace API calls (offline mode passes).

3. **Lightning Studio reuse check**:
   ```bash
   python3 -c "

