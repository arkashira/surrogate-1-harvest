# airship / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement:** Deterministic CDN-only data loading + Lightning Studio lifecycle resilience for Surrogate-1 training.  
**Why:** Eliminates HF API 429s during data loads and prevents quota loss from idle-stop/studio recreation.

### Scope
- Frontend (Airship UI) changes only: add a small training orchestration panel + status indicators.
- Backend-agnostic: produces a `file_manifest.json` and a reusable `train.py` launcher that Lightning Studio can run with zero HF API calls during training.
- Fits <2h: ~20 min for manifest script, ~30 min for train.py + Docker entrypoint, ~40 min for Airship UI panel, ~30 min test/validate.

---

### 1) File manifest generator (run once from Mac/orchestrator)

`/opt/axentx/airship/scripts/generate_cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a deterministic CDN-only manifest for a single date folder.
Run from orchestrator after rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime

# HF Hub: use huggingface_hub only for listing (single call), not during training
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-org/surrogate-1-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUTPUT = os.getenv("MANIFEST_OUT", "/opt/axentx/airship/surrogate/data/cdn_manifest.json")

def main() -> None:
    api = HfApi()
    try:
        tree = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    except Exception as e:
        print(f"Failed to list repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    files = sorted([f.rfilename for f in tree if f.type == "file"])
    if not files:
        print(f"No files found in {HF_REPO}/{DATE_FOLDER}", file=sys.stderr)
        sys.exit(1)

    manifest = {
        "repo": HF_REPO,
        "date": DATE_FOLDER,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "cdn_prefix": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{DATE_FOLDER}"
    }

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {OUTPUT}")

if __name__ == "__main__":
    main()
```

**Make executable:**
```bash
chmod +x /opt/axentx/airship/scripts/generate_cdn_manifest.py
```

---

### 2) CDN-only dataset loader (used inside Lightning training)

`/opt/axentx/airship/surrogate/data/cdn_dataset.py`

```python
import json
import os
from typing import Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: str, max_files: int = None):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]
        if max_files:
            self.files = self.files[:max_files]
        self.prefix = self.manifest["cdn_prefix"]

    def _stream_parquet(self, filename: str) -> pa.Table:
        url = f"{self.prefix}/{filename}"
        # CDN download: no auth header, bypasses /api/ rate limits
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return pq.read_table(pa.BufferReader(resp.content)).select(["prompt", "response"])

    def __iter__(self):
        for fn in self.files:
            try:
                table = self._stream_parquet(fn)
                for row in table.to_pylist():
                    # Ensure schema: prompt/response only
                    yield {
                        "prompt": str(row.get("prompt", "")),
                        "response": str(row.get("response", ""))
                    }
            except Exception as exc:
                # Log and skip bad files; don't crash training
                print(f"Skipping {fn}: {exc}")
                continue
```

---

### 3) Lightning Studio lifecycle wrapper (reuses running studios)

`/opt/axentx/airship/surrogate/train/lightning_launcher.py`

```python
import time
from lightning import LightningWork, LightningFlow, LightningApp, Machine
from lightning.fabric.utilities.cloud_io import _is_local
from surrogate.data.cdn_dataset import CDNParquetDataset

SURROGATE_STUDIO_NAME = "surrogate-l40s-training"
MANIFEST_PATH = "/opt/axentx/airship/surrogate/data/cdn_manifest.json"

class SurrogateTrainer(LightningWork):
    def __init__(self):
        super().__init__(
            cloud_compute=Machine("L40S"),
            cloud_build_commands=[
                "pip install pyarrow requests torch transformers datasets"
            ],
        )
        self._studio_reused = False

    def run(self):
        # Reuse running studio if available (saves quota)
        from lightning import Teamspace
        for s in Teamspace.studios:
            if s.name == SURROGATE_STUDIO_NAME and s.status == "Running":
                print(f"Reusing running studio: {s.name}")
                self._studio_reused = True
                break

        dataset = CDNParquetDataset(MANIFEST_PATH)
        # Your training loop here, e.g. HF Trainer with dataset
        print("Starting Surrogate-1 training with CDN-only data...")
        # trainer = Trainer(...)
        # trainer.train(...)

    def on_exit(self):
        # Optional: stop studio to avoid idle charges, or keep running for quick reruns
        pass

class RootFlow(LightningFlow):
    def __init__(self):
        super().__init__()
        self.trainer = SurrogateTrainer()

    def configure_layout(self):
        return [{"name": "train", "content": self.trainer}]

    def run(self):
        if not self.trainer.has_started:
            self.trainer.start()

if __name__ == "__main__":
    # Mac orchestration only; actual compute on Lightning
    if _is_local():
        print("Launching Lightning App (training runs on cloud)...")
    app = LightningApp(RootFlow())
```

---

### 4) Entrypoint script (Mac orchestration)

`/opt/axentx/airship/scripts/run_surrogate_training.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
export SHELL=/bin/bash

cd /opt/axentx/airship

# 1) Generate manifest once per date folder
./scripts/generate_cdn_manifest.py

# 2) Launch Lightning App (reuses studio, CDN-only data)
python3 surrogate/train/lightning_launcher.py
```

**Make executable:**
```bash
chmod +x /opt/axentx/airship/scripts/run_surrogate_training.sh
```

---

### 5) Crontab example (if scheduling)

```cron
# Edit with: crontab -e
SHELL=/bin/bash
0 2 * * * /opt/axentx/airship/scripts/run_surrogate_training.sh >> /var/log/airship/surrogate_training.log 2>&1
```

---

### 6) Quick test (no Lightning)

`/opt/axentx/airship/surrogate/test_cdn.py`

```python
from surrogate.data.cdn_dataset import CDNParquetDataset
ds = CDNParquetDataset("/opt/axentx/airship/surrogate/data/cdn_manifest.json", max_files=2)
for item in ds:
    print(item["prompt"][:80], "...")
```

---

### 7) Airship UI panel (minimal)

Add to your Airship frontend (e.g., React component) to show status and trigger:

```tsx
// components/TrainingPanel.tsx
import {
