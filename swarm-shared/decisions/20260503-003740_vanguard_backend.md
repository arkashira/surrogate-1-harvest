# vanguard / backend

### 1. Diagnosis (consolidated)
- **No persisted `(repo, dateFolder)` file manifest** → every run re-enumerates via authenticated HF API → quota burn and 429 risk.  
- **Recursive enumeration / `load_dataset(streaming=True)` on heterogeneous repos** → amplifies rate-limit exposure and can trigger pyarrow schema errors.  
- **No CDN bypass** → training fetches go through `/api/` endpoints subject to strict rate limits instead of high-throughput CDN.  
- **Lightning Studio recreation** → burns quota (80+ hrs/mo) instead of reusing running studios.  
- **Missing wrapper hygiene** (shebang, executable bit, `SHELL=/bin/bash` for cron) → cron-launched jobs fail silently.  

### 2. Proposed change (single coherent plan)
Create `/opt/axentx/vanguard/backend/manifest.py`, patch `/opt/axentx/vanguard/backend/train.py`, and add `/opt/axentx/vanguard/backend/run_training.sh`:
- **`build_manifest(repo, date_folder)`**: single non-recursive `list_repo_tree` call per folder → writes `manifests/{repo_safe}/{date_folder}.json`.  
- **Training data loader**: reads manifest and fetches only via CDN (`resolve/main/...`) with zero auth during training.  
- **Studio reuse**: `get_or_create_studio(name, machine)` reuses running studios to avoid quota waste.  
- **Wrapper script**: proper shebang, `set -euo pipefail`, `exec`, `chmod +x`, and `SHELL=/bin/bash` comment for reliable cron execution.  

### 3. Implementation

```bash
# /opt/axentx/vanguard/backend/run_training.sh
#!/usr/bin/env bash
# SHELL=/bin/bash  (ensure cron uses bash)
set -euo pipefail
cd /opt/axentx/vanguard/backend
exec python train.py "$@"
```

```bash
chmod +x /opt/axentx/vanguard/backend/run_training.sh
```

```python
# /opt/axentx/vanguard/backend/manifest.py
import json
import os
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi, list_repo_tree

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def _safe_repo_name(repo: str) -> str:
    return repo.replace("/", "_")

def build_manifest(repo: str, date_folder: str, token: str = None) -> Path:
    """
    Single authenticated API call to list one date folder (non-recursive).
    Writes manifest JSON and returns its path.
    """
    api = HfApi(token=token)
    entries = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = sorted(e.path for e in entries if e.type == "file")

    manifest: Dict[str, object] = {
        "repo": repo,
        "date_folder": date_folder,
        "files": files,
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main"
    }

    out = MANIFEST_DIR / _safe_repo_name(repo) / f"{date_folder}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    return out

def load_manifest(repo: str, date_folder: str) -> Dict[str, object]:
    p = MANIFEST_DIR / _safe_repo_name(repo) / f"{date_folder}.json"
    if not p.exists():
        raise FileNotFoundError(f"Manifest missing: {p}. Run build_manifest first.")
    return json.loads(p.read_text())
```

```python
# /opt/axentx/vanguard/backend/train.py  (patched excerpt)
import os
import json
import requests
import io
from pathlib import Path
from typing import List, Dict, Any

import pyarrow.parquet as pq
import torch
from torch.utils.data import Dataset, DataLoader
from lightning.pytorch import Trainer, LightningModule
from lightning.pytorch.studio import Studio, Machine, Teamspace

from .manifest import build_manifest, load_manifest

HF_TOKEN = os.getenv("HF_TOKEN")
REPO = "axentx/surrogate-1-data"
DATE_FOLDER = "2026-04-29"

# ---- Studio reuse ----
def get_or_create_studio(name: str, machine: Machine = Machine.L40S) -> Studio:
    for s in Teamspace.studios():
        if getattr(s, "name", None) == name and getattr(s, "status", None) == "Running":
            return s
    return Studio.create(
        name=name,
        machine=machine,
        teamspace="axentx",
        create_ok=True,
    )

# ---- CDN fetch ----
def cdn_fetch(repo: str, file_path: str, timeout: float = 30.0) -> bytes:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

# ---- Parquet loader ----
def parquet_to_samples(content: bytes, columns: List[str] = ("prompt", "response")) -> List[Dict[str, Any]]:
    table = pq.read_table(io.BytesIO(content), columns=columns)
    return table.to_pylist()

# ---- Manifest-based dataset ----
class ManifestParquetDataset(Dataset):
    def __init__(self, manifest_path: Path, repo: str, columns: List[str] = ("prompt", "response")):
        manifest = json.loads(manifest_path.read_text())
        self.repo = manifest["repo"]
        self.files = [f for f in manifest["files"] if f.endswith(".parquet")]
        self.columns = columns

    def __len__(self) -> int:
        return len(self.files)

    def __getitem__(self, idx: int) -> List[Dict[str, Any]]:
        f = self.files[idx]
        content = cdn_fetch(self.repo, f)
        return parquet_to_samples(content, self.columns)

def train_dataloader(manifest_path: Path, batch_size: int = 8, num_workers: int = 4) -> DataLoader:
    dataset = ManifestParquetDataset(manifest_path, REPO)
    return DataLoader(dataset, batch_size=batch_size, num_workers=num_workers, collate_fn=lambda x: x)

# ---- Entrypoint ----
if __name__ == "__main__":
    # One-time manifest generation (or run from orchestration)
    manifest_path = build_manifest(REPO, DATE_FOLDER, token=HF_TOKEN)
    print(f"Manifest written: {manifest_path}")

    # Reuse studio to avoid quota burn
    studio = get_or_create_studio("vanguard-surrogate-train")
    if studio.status != "Running":
        studio.start(machine="L40S")

    # Quick local test of CDN-only dataloader
    samples_batch = next(iter(train_dataloader(manifest_path, batch_size=1, num_workers=0)))
    print(f"Loaded batch with {len(samples_batch)} file(s); first file samples: {len(samples_batch[0])}")
```

### 4. Verification checklist
- [ ] `run_training.sh` is executable (`chmod +x`) and starts training with `exec python train.py`.  
- [ ] `build_manifest` writes valid `manifests/{repo_safe}/{date_folder}.json` after one non-recursive API call.  
- [ ] `train.py` loads manifest and fetches only via CDN URLs (no authenticated calls during training).  
- [ ] `get_or_create_studio` reuses a running studio instead of creating a new one each run.  
- [ ] API call count and quota usage drop; no 429 errors during training.  
- [ ] Training completes successfully with correct data shapes and no pyarrow schema errors.
