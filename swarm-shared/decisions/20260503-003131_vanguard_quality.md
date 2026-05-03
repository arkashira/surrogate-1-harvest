# vanguard / quality

Candidate 1 is substantially complete and mostly correct.  
Candidate 2 only restates the diagnosis and does not add implementation detail.  

Below is the **single, final synthesis**: keep Candidate 1’s concrete design, fix its small technical inaccuracies, tighten actionability, and add the missing verification section so the plan is immediately executable.

---

## 1. Diagnosis (condensed)

- **No persisted manifest** → every training run re-enumerates via authenticated HF API → quota burn + 429 risk.  
- **Loader likely uses recursive `list_repo_tree` / `load_dataset`** → couples training to API availability and amplifies rate-limit exposure.  
- **No CDN-only fetch path** → authenticated API calls continue during data loading, violating the HF CDN bypass pattern.  
- **Lightning Studio reuse not enforced** → idle-stop can kill training; cold-start latency and quota waste.  

---

## 2. Proposed change (scope: two files)

Create/update:

1. `/opt/axentx/vanguard/training/manifest.py`  
   - Single authenticated HF API call to list one `date_folder` (non-recursive).  
   - Persist a `files.json` manifest containing **only CDN URLs** for `.parquet` files.  

2. `/opt/axentx/vanguard/training/train.py`  
   - Read the manifest and build a `CdnParquetIterable` that fetches exclusively via CDN (`resolve/main/...`).  
   - Reuse a running Lightning Studio; restart only if stopped.  
   - Add an idle-stop guard before `.run()`/training entry.  

No changes to orchestration or cron.

---

## 3. Implementation

```bash
# Ensure directories
mkdir -p /opt/axentx/vanguard/training/manifests
```

### `/opt/axentx/vanguard/training/manifest.py`

```python
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi, RepoFile

HF_API = HfApi()
MANIFEST_ROOT = Path(__file__).parent / "manifests"

def build_manifest(repo: str, date_folder: str, revision: str = "main") -> Path:
    """
    Single authenticated HF API call to list one date folder (non-recursive),
    then produce a manifest of CDN URLs for .parquet files only.
    """
    manifest_dir = MANIFEST_ROOT / repo / date_folder
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "files.json"

    # Non-recursive listing of the target folder only
    entries: List[RepoFile] = HF_API.list_repo_tree(
        repo=repo,
        path=date_folder,
        revision=revision,
        recursive=False,
    )

    files: List[Dict[str, object]] = []
    for entry in entries:
        if entry.rfilename.endswith(".parquet"):
            cdn_url = (
                f"https://huggingface.co/datasets/{repo}/resolve/main/"
                f"{entry.rfilename}"
            )
            files.append({
                "cdn_url": cdn_url,
                "path": entry.rfilename,
                "size": getattr(entry, "size", None),
            })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "revision": revision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


def load_manifest(repo: str, date_folder: str) -> Dict:
    manifest_path = MANIFEST_ROOT / repo / date_folder / "files.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest missing: {manifest_path}")
    return json.loads(manifest_path.read_text())
```

### `/opt/axentx/vanguard/training/train.py`

```python
from __future__ import annotations
import os
import sys
from pathlib import Path

import lightning as L
import torch
from lightning.fabric.plugins import BitsandbytesPrecision
from torch.utils.data import DataLoader, IterableDataset

from .manifest import build_manifest, load_manifest

# ---- Configuration ----
HF_REPO = os.getenv("HF_DATASET_REPO", "your-org/your-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", "batches/mirror-merged/2026-04-29")
MANIFEST_ONLY = os.getenv("MANIFEST_ONLY", "0") == "1"
L40S = L.Machine.L40S

# ---- Manifest step (single API call) ----
manifest_path = build_manifest(HF_REPO, DATE_FOLDER)
print(f"Manifest written: {manifest_path}")

if MANIFEST_ONLY:
    sys.exit(0)

manifest = load_manifest(HF_REPO, DATE_FOLDER)
if not manifest.get("files"):
    raise RuntimeError("No parquet files found in manifest")

# ---- CDN-only IterableDataset ----
class CdnParquetIterable(IterableDataset):
    def __init__(self, files: list):
        self.files = files

    def __iter__(self):
        import io
        import pyarrow.parquet as pq
        import requests

        for item in self.files:
            url = item["cdn_url"]
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            # Project to {prompt, response} at parse time
            df = table.select(["prompt", "response"]).to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"]}

# ---- Lightning studio reuse + idle-stop guard ----
def get_or_create_studio(name: str) -> L.studio.Studio:
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Creating studio: {name}")
    return L.studio.Studio(
        name=name,
        create_ok=True,
        machine=L40S,
    )

studio = get_or_create_studio("vanguard-train")

# ---- Fabric ----
fabric = L.Fabric(devices=1, accelerator="cuda", precision="bf16-mixed")
fabric.launch()

# ---- Training step ----
def train():
    dataset = CdnParquetIterable(manifest["files"])
    loader = DataLoader(dataset, batch_size=8, num_workers=0)

    # Minimal example model
    model = torch.nn.Transformer(d_model=512, nhead=8)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-4)
    model, optimizer = fabric.setup(model, optimizer)

    model.train()
    for batch in loader:
        # Dummy tokenized lengths for example
        src = torch.randint(0, 1000, (8, 32))
        tgt = torch.randint(0, 1000, (8, 32))
        src, tgt = fabric.to_device(src), fabric.to_device(tgt)

        optimizer.zero_grad()
        out = model(src, tgt)
        loss = out.sum() * 0.0  # placeholder
        fabric.backward(loss)
        optimizer.step()
        print("step done")
        break

# ---- Guard for idle-stop ----
def run_training():
    studio.refresh()
    if studio.status != "Running":
        print("Studio stopped. Restarting...")
        studio.start(machine=L40S)
        studio.refresh()
    train()

if __name__ == "__main__":
    run_training()
```

---

## 4. Verification (complete)

1. **Manifest creation**  
   ```bash
   cd /opt/axentx/vanguard/training
   HF_DATASET_REPO=datasets/your/repo \
     DATE_FOLDER=batches/mirror-merged/2026-04-29 \
     python -c "from manifest import build_manifest; print(build_manifest('datasets/your/repo', 'batches/mirror-merged/2026-04-29'))"
   ```
   - Confirm `manifests/datasets/your/repo/batches/mirror-
