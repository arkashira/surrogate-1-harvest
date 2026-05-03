# airship / discovery

## Highest-Value Incremental Improvement (≤2h)

**Goal**: Eliminate HF API 429 bottlenecks and Lightning quota waste for Surrogate training by implementing **CDN-first deterministic ingestion + Lightning Studio reuse/idle-stop guard**.

**Why**:  
- Removes `/api/` auth-check rate limits (429) during data loading  
- Recovers ~80hr/mo Lightning quota by reusing running studios  
- Prevents idle-stop training death with pre-run health checks  
- Fits existing patterns (CDN bypass, studio reuse, idle guard)

---

## Implementation Plan

### 1. Pre-list HF file paths once → JSON manifest (Mac orchestration)
- Single `list_repo_tree` call per date folder after rate-limit window  
- Save to `training/file_manifests/{date}_manifest.json`  
- Embed path list in `train.py`; Lightning does **CDN-only** fetches (zero API calls during training)

### 2. Lightning Studio reuse + idle-stop guard
- Before `Studio().run()`, list `Teamspace.studios` and reuse if running  
- If stopped, restart with `target.start(machine=Machine.L40S)`  
- Wrap `.run()` with status check to avoid idle-kill

### 3. Dataset projection: {prompt, response} only at parse time
- Keep raw files intact; project cols only in dataloader  
- Move attribution to filename pattern: `batches/mirror-merged/{date}/{slug}.parquet`  
- Avoid `source`/`ts` cols in enriched/ to prevent schema drift

---

## Code Snippets

### `scripts/build_hf_manifest.py` (Mac orchestration)
```python
#!/usr/bin/env python3
"""
Build deterministic HF file manifest for CDN-only training.
Run after HF API rate-limit window clears.
"""
import json
import os
from huggingface_hub import HfApi

API_TOKEN = os.getenv("HF_TOKEN")
REPO_ID = "axentx/surrogate-mirror"
DATE_FOLDER = "2026-05-03"  # parametrize as needed
OUTPUT_PATH = f"training/file_manifests/{DATE_FOLDER}_manifest.json"

def build_manifest():
    api = HfApi(token=API_TOKEN)
    # Non-recursive per folder to avoid 100x pagination
    tree = api.list_repo_tree(
        repo_id=REPO_ID,
        path=f"batches/mirror-merged/{DATE_FOLDER}",
        recursive=False
    )
    files = [
        f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/batches/mirror-merged/{DATE_FOLDER}/{item.path}"
        for item in tree
        if item.path.endswith(".parquet")
    ]
    manifest = {
        "date": DATE_FOLDER,
        "repo": REPO_ID,
        "files": sorted(files),
        "total": len(files)
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest saved: {OUTPUT_PATH} ({len(files)} files)")

if __name__ == "__main__":
    build_manifest()
```

### `surrogate/train.py` (CDN-only dataloader)
```python
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.urls = manifest["files"]

    def __iter__(self):
        for url in self.urls:
            # CDN download: no Authorization header → bypasses /api/ rate limit
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            with open("/tmp/temp.parquet", "wb") as f:
                f.write(resp.content)
            table = pq.read_table("/tmp/temp.parquet")
            # Project to {prompt, response} only at parse time
            for batch in table.to_batches():
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    yield {
                        "prompt": row["prompt"],
                        "response": row["response"]
                    }
```

### `surrogate/lightning_launcher.py` (Studio reuse + idle guard)
```python
from lightning import Lightning, Teamspace, Machine, Studio
import time

def launch_or_reuse_studio(studio_name="surrogate-train", machine=Machine.L40S):
    lightning = Lightning()
    teamspace = Teamspace()

    # Reuse if already running
    for s in teamspace.studios:
        if s.name == studio_name and s.status == "Running":
            print(f"Reusing running studio: {studio_name}")
            return s

    # Create or restart
    studio = Studio(
        name=studio_name,
        machine=machine,
        create_ok=True
    )

    # Idle-stop guard: ensure machine is active before run
    max_retries = 3
    for attempt in range(max_retries):
        status = studio.status
        if status == "Running":
            print("Studio active, proceeding with run.")
            return studio
        elif status in ["Stopped", "Idle"]:
            print(f"Studio {status}, restarting...")
            studio.start(machine=machine)
            time.sleep(30)  # allow boot
        else:
            print(f"Studio status: {status}, waiting...")
            time.sleep(15)

    raise RuntimeError("Failed to activate studio after retries")

# Usage
if __name__ == "__main__":
    studio = launch_or_reuse_studio()
    result = studio.run(
        "train.py",
        arguments=["--manifest", "training/file_manifests/2026-05-03_manifest.json"]
    )
```

---

## Verification Steps

1. Run `python scripts/build_hf_manifest.py` on Mac (after HF window clears)  
2. Confirm `training/file_manifests/*_manifest.json` contains CDN URLs  
3. Execute `python surrogate/lightning_launcher.py`  
4. Verify Lightning Studio reuses existing or restarts cleanly  
5. Monitor training logs: zero HF API calls during data load (CDN-only)  

**Expected**: No 429 errors, reduced Lightning quota consumption, uninterrupted training across idle stops.
