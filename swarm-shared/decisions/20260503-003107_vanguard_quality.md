# vanguard / quality

## 1. Diagnosis

- No persisted `(repo, dateFolder)` manifest exists → every training run re-enumerates via authenticated HF API → quota burn and 429 risk.
- Data loader likely uses recursive `list_repo_files` or `load_dataset` during training → amplifies rate-limit exposure and couples training to API availability.
- No CDN-only fetch path in training; authenticated API calls continue during data loading, violating the CDN bypass pattern.
- Lightning Studio reuse is not enforced; idle-stop kills training and quota is wasted by recreating studios instead of reusing running ones.
- No deterministic fallback when Lightning Studio is stopped; training fails instead of restarting on an available machine.

## 2. Proposed change

Add a lightweight manifest-and-train orchestration layer:

- File: `/opt/axentx/vanguard/train.py` (create or patch)
- Scope:
  - Single authenticated HF API call per `(repo, dateFolder)` to produce `manifests/{repo}/{dateFolder}.json` listing CDN URLs.
  - Training script reads the manifest and uses only CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with `requests`/`pyarrow` — zero HF API calls during training.
  - Reuse running Lightning Studio if present; otherwise start one deterministically (L40S free tier fallback).
  - Guard `.run()` calls with status checks and auto-restart on idle-stop.

## 3. Implementation

```python
#!/usr/bin/env python3
"""
vanguard/train.py
- Persist (repo, dateFolder) manifest once.
- Train using CDN-only URLs (no HF API during data load).
- Reuse or start Lightning Studio safely.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict

import lightning as L
import pyarrow.parquet as pq
import requests
from lightning.fabric.utilities.cloud_io import _load_from_remote as _lightning_load

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "your-org/your-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
MANIFEST_ROOT = Path(__file__).parent / "manifests"
MANIFEST_PATH = MANIFEST_ROOT / HF_DATASET_REPO / f"{DATE_FOLDER}.json"

# ----------
# 1) Manifest: one authenticated API call per (repo, dateFolder)
# ----------
def build_manifest(repo: str, date_folder: str) -> List[Dict]:
    """
    Single HF API call to list non-recursive folder; persist manifest.
    Uses HF API only here. All later training uses CDN URLs.
    """
    from huggingface_hub import HfApi

    api = HfApi()
    # Non-recursive listing to avoid pagination explosion
    files = api.list_repo_tree(repo, path=date_folder, recursive=False)

    entries = []
    for f in files:
        if f.rfilename.endswith(".parquet"):
            cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{f.rfilename}"
            entries.append({"path": f.rfilename, "cdn_url": cdn_url})

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w") as fp:
        json.dump(entries, fp, indent=2)
    return entries

def load_manifest(repo: str, date_folder: str) -> List[Dict]:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as fp:
            return json.load(fp)
    return build_manifest(repo, date_folder)

# ----------
# 2) Data loader: CDN-only, no HF API
# ----------
def stream_parquet_rows(entries: List[Dict], batch_size: int = 1024):
    """
    Yield rows from parquet files via CDN URLs.
    No Hugging Face API calls during training.
    """
    for entry in entries:
        url = entry["cdn_url"]
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        table = pq.read_table(pq.ParquetFile(pq.ParquetBuffer(resp.content)))
        df = table.to_pandas()
        # Project to {prompt, response} at parse time (schema normalization)
        for _, row in df.iterrows():
            yield {
                "prompt": str(row.get("prompt", row.get("input", ""))),
                "response": str(row.get("response", row.get("output", ""))),
            }

# ----------
# 3) Lightning Studio orchestration
# ----------
def get_or_create_studio(name: str = "vanguard-train", machine: str = "L40S"):
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}")
            return s

    print(f"No running studio '{name}' found. Creating...")
    # Free tier fallback order: try L40S (free tier), avoid H200 unless paid account
    return L.Studio(
        name=name,
        create_ok=True,
        machine=machine,  # L40S available on lightning-public-prod
    )

def run_training_on_studio(studio, manifest_entries):
    """
    Guarded run: checks studio status and restarts if stopped (idle-timeout).
    """
    if studio.status != "running":
        print(f"Studio stopped (status={studio.status}). Restarting...")
        # Reuse same machine type; fallback handled by get_or_create_studio
        studio.start(machine="L40S")

    # Example training action (replace with your Trainer loop)
    # This runs inside the Studio environment via .run()
    def _train():
        examples = list(stream_parquet_rows(manifest_entries, batch_size=512))
        print(f"Loaded {len(examples)} examples via CDN for training step")
        # Insert your model/trainer logic here.
        # Do NOT call HF API (load_dataset/list_repo_*) inside this function.

    studio.run(_train)

# ----------
# 4) Entrypoint
# ----------
def main():
    entries = load_manifest(HF_DATASET_REPO, DATE_FOLDER)
    print(f"Manifest ready: {len(entries)} parquet files from {HF_DATASET_REPO}/{DATE_FOLDER}")

    studio = get_or_create_studio(name="vanguard-train", machine="L40S")
    run_training_on_studio(studio, entries)

if __name__ == "__main__":
    main()
```

## 4. Verification

1. **Manifest creation**  
   ```bash
   cd /opt/axentx/vanguard
   HF_DATASET_REPO=your-org/your-dataset DATE_FOLDER=2026-05-01 python train.py
   ```
   - Confirm `manifests/your-org/your-dataset/2026-05-01.json` exists and contains CDN URLs.
   - Confirm no additional HF API calls occur after manifest creation (check logs / HF rate-limit headers).

2. **CDN-only data loading**  
   - Temporarily block HF API credentials or revoke token and rerun; training should still load data via CDN (manifest already present).

3. **Lightning Studio reuse**  
   - Start a studio manually in the same teamspace named `vanguard-train` and set it to Running.  
   - Rerun script; verify log shows `Reusing running studio`.

4. **Idle-stop recovery**  
   - Stop the studio externally, then rerun script; verify log shows `Studio stopped... Restarting...` and training proceeds.

5. **Schema projection**  
   - Confirm row outputs contain only `prompt` and `response` keys and no extra metadata columns (e.g., `source`, `ts`) in the yielded examples.
