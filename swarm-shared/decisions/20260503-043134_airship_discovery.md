# airship / discovery

## Implementation Plan (≤2h)

**Highest-value incremental improvement:** Deterministic CDN file manifest generator + Lightning Studio lifecycle resilience for Surrogate-1 training.

**Why:** Eliminates HF API 429s during Surrogate-1 training and prevents quota loss from idle-timeout kills.

---

### 1) Pre-list date folder → JSON manifest (Mac orchestration)
- Single `list_repo_tree(recursive=False)` for one date folder (e.g. `batches/mirror-merged/2026-05-03/`)
- Save list to `manifests/2026-05-03.json`
- Embed in `train.py`; Lightning training does CDN-only fetches with zero API calls during data load

### 2) Lightning Studio lifecycle resilience
- Reuse running studios (`Teamspace.studios` lookup)
- Before each `.run()`, check status; if stopped, restart with `target.start(machine=Machine.L40S)`
- Prevents idle-timeout training death

### 3) Training script updates
- Use CDN URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`
- Project to `{prompt, response}` only at parse time
- Attribution via filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`)

---

## Code Snippets

### `scripts/generate_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate deterministic CDN manifest for a date folder.
Run from Mac (or any orchestration host) after rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_DIR = Path(os.getenv("OUT_DIR", "manifests"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

def main():
    api = HfApi()
    folder_path = f"batches/mirror-merged/{DATE_FOLDER}"
    print(f"Listing {HF_REPO}/{folder_path} ...")

    try:
        files = api.list_repo_tree(
            repo_id=HF_REPO,
            path=folder_path,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"Error listing repo tree: {e}")
        sys.exit(1)

    # Keep only parquet files; store relative path for CDN fetch
    entries = [
        {
            "path": f"{folder_path}/{f.rfilename}",
            "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{folder_path}/{f.rfilename}",
            "size": getattr(f, "size", None),
        }
        for f in files
        if f.rfilename.endswith(".parquet")
    ]

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "count": len(entries),
        "entries": entries,
    }

    out_path = OUT_DIR / f"{DATE_FOLDER}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path} ({len(entries)} files)")

if __name__ == "__main__":
    main()
```

### `surrogate/train.py` (excerpt)
```python
import json
from pathlib import Path
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

MANIFEST_PATH = Path("manifests/2026-05-03.json")  # or injected via env

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: Path):
        manifest = json.loads(manifest_path.read_text())
        self.files = [e["cdn_url"] for e in manifest["entries"]]

    def __iter__(self):
        for url in self.files:
            # CDN fetch: no Authorization header, bypasses API rate limits
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            table = pq.read_table(pq.ParquetFile(pq.BufferReader(resp.content)))
            # Project to {prompt, response} only
            for batch in table.to_batches():
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    yield {
                        "prompt": row.get("prompt"),
                        "response": row.get("response"),
                    }
```

### `surrogate/lightning_launcher.py` (excerpt)
```python
from lightning import Fabric, LightningFlow, LightningWork, Machine
from lightning.app import LightningApp
from lightning.app.utilities.state import AppState

class SurrogateTrainer(LightningWork):
    def __init__(self):
        super().__init__()
        self.cloud = None
        self.studio = None

    def run(self):
        from lightning import Teamspace

        # Reuse running studio if available
        for s in Teamspace.studios:
            if s.name == "surrogate-trainer" and s.status == "running":
                self.studio = s
                print(f"Reusing running studio: {s.name}")
                break
        else:
            # Create new studio (L40S in free tier; H200 requires lightning-lambda-prod)
            from lightning import Studio
            self.studio = Studio(
                name="surrogate-trainer",
                machine=Machine.L40S,
                cloud="lightning-public-prod",
                create_ok=True,
            )

        # Ensure studio is running before submitting work
        if self.studio.status != "running":
            print(f"Studio stopped; restarting on L40S")
            self.studio.start(machine=Machine.L40S)

        # Submit training job (CDN-only data loading)
        self.studio.run(
            "python train.py",
            working_dir=".",
        )

# In your LightningFlow/LightningApp, include SurrogateTrainer
```

### `Makefile` (orchestration helpers)
```make
.PHONY: manifest train

manifest:
	@python scripts/generate_manifest.py

train: manifest
	@cd surrogate && python lightning_launcher.py
```

---

## Deployment Steps (≤2h)

1. **Create manifest directory**
   ```bash
   mkdir -p manifests
   ```

2. **Generate manifest** (run once per date folder)
   ```bash
   python scripts/generate_manifest.py
   ```

3. **Update training config** to reference manifest path via env:
   ```bash
   export MANIFEST_PATH="manifests/2026-05-03.json"
   ```

4. **Launch Lightning training** (reuses studio, resilient to idle timeout)
   ```bash
   cd surrogate && python lightning_launcher.py
   ```

5. **Verify** training runs with CDN-only fetches (no HF API calls during data load).

---

**Expected outcome:**  
- Zero HF API 429s during training (CDN bypass)  
- No lost quota from idle-timeout studio kills (reuse + restart logic)  
- Deterministic file list embedded in training script  
- Clean projection to `{prompt, response}` with attribution via filename pattern
