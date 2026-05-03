# vanguard / discovery

## Final consolidated solution

**Core diagnosis (unified)**
- Frontend/training still triggers authenticated `list_repo_tree` (and `/api/` proxies) on load, burning HF quota (1000/5min) and causing 429s.
- No persisted `(repo, dateFolder)` file-list; every reload re-enumerates via API instead of embedding a static manifest for CDN-only training.
- File fetches use authenticated `/api/` paths when public CDN URLs should be used to bypass auth and rate limits entirely.
- No guard to reuse an already-running Lightning Studio; training likely recreates studios and wastes quota (~80hr/mo) and hits idle-stop/timeout issues.
- Surrogate-1 ingestion likely still uses `load_dataset(streaming=True)` on heterogeneous repos, risking `pyarrow.CastError` from mixed schemas.

**Single source of truth**
- One-time manifest generation on Mac/CI (after rate-limit window) produces a static JSON listing only the parquet files for a given `(repo, dateFolder)`.
- Training uses **only** public CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization headers.
- Lightning Studio reuse + idle-stop guard ensures we don’t recreate studios unnecessarily and can restart stopped machines automatically.
- Safe per-file ingestion projects only `{prompt,response}` and ignores unknown columns to avoid mixed-schema errors.

---

## 1. Manifest builder (run once per date folder)

File: `/opt/axentx/vanguard/scripts/discover_file_list.py`

```python
#!/usr/bin/env python3
"""
Generate file-list.json for a (repo, dateFolder) to enable CDN-only training.
Run from Mac/CI after HF rate-limit window clears.
"""
import json
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/surrogate-1")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", "2026-04-29")  # e.g. batches/mirror-merged/2026-04-29
OUT_PATH = Path(os.getenv("OUT_PATH", "file-list.json")).resolve()

def main() -> None:
    api = HfApi()
    prefix = f"{DATE_FOLDER}/"
    # Single non-recursive call per folder (avoids 100x pagination)
    files = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    paths = [
        f.rfilename for f in files
        if f.rfilename.endswith(".parquet") and f.rfilename.startswith(prefix)
    ]
    if not paths:
        print(f"No parquet files found under {DATE_FOLDER} in {HF_REPO}", file=sys.stderr)
        sys.exit(1)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as fp:
        json.dump(paths, fp, indent=2)
    print(f"Wrote {len(paths)} parquet paths to {OUT_PATH}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/discover_file_list.py
```

Usage (one-time):

```bash
cd /opt/axentx/vanguard
HF_DATASET_REPO=datasets/axentx/surrogate-1 \
HF_DATE_FOLDER=2026-04-29 \
OUT_PATH=file-list.json \
python scripts/discover_file_list.py
```

---

## 2. Safe HF ingestion helper (per-file, schema-safe)

File: `/opt/axentx/vanguard/ingest/hf_safe_ingest.py`

```python
import pyarrow.parquet as pq
import requests
from typing import Iterator, List, Dict, Any

CDN_BASE = "https://huggingface.co/datasets"

def build_cdn_url(repo: str, filepath: str) -> str:
    # Public CDN URL — no Authorization header required
    repo_key = repo.removeprefix("datasets/")
    return f"{CDN_BASE}/{repo_key}/resolve/main/{filepath}"

def stream_parquet_rows(url: str, columns=("prompt", "response")) -> Iterator[List[Dict[str, Any]]]:
    """
    Stream rows from a remote parquet file, projecting only `columns`.
    Ignores unknown/mixed columns safely.
    """
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        # Use raw stream for zero-copy where possible
        with pq.ParquetFile(pq.ParquetFile(r.raw)) as pf:
            available = [c for c in pf.schema.names if c in columns]
            if not available:
                return
            for batch in pf.iter_batches(columns=available, batch_size=512):
                yield batch.to_pylist()
```

---

## 3. Lightning launcher with studio reuse + idle-stop guard

File: `/opt/axentx/vanguard/ops/lightning_launcher.py`

```python
import os
from pathlib import Path

from lightning import LightningWork, LightningFlow, LightningApp, Machine
from axentx.vanguard.ingest.hf_safe_ingest import build_cdn_url, stream_parquet_rows

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/surrogate-1")
MANIFEST_PATH = Path(__file__).parent.parent / "file-list.json"

def load_file_list():
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"Manifest not found: {MANIFEST_PATH}. "
            "Run scripts/discover_file_list.py first."
        )
    with MANIFEST_PATH.open() as f:
        return json.load(f)

import json

class SurrogateTrainer(LightningWork):
    def __init__(self):
        super().__init__(machine=Machine.L40S, cloud="lightning-public-prod")

    def run(self):
        file_list = load_file_list()
        for rel_path in file_list:
            url = build_cdn_url(HF_DATASET_REPO, rel_path)
            for rows in stream_parquet_rows(url):
                self.train_step(rows)

    def train_step(self, rows):
        # Replace with your actual training logic
        for item in rows:
            _ = item.get("prompt"), item.get("response")
        # e.g., self.model.train_on_batch(...)

class VanguardFlow(LightningFlow):
    def __init__(self):
        super().__init__()
        self.trainer = SurrogateTrainer()

    def configure_layout(self):
        return []

    def run(self):
        # Guard: restart studio if stopped (idle-timeout kills training)
        if getattr(self.trainer, "status", None) == "stopped":
            self.trainer.start(machine=Machine.L40S)
        self.trainer.run()

app = LightningApp(VanguardFlow())
```

---

## 4. Consolidated training entrypoint (lightweight)

File: `/opt/axentx/vanguard/train.py`

```python
"""
Lightweight CDN-only training entrypoint.
Uses manifest + safe ingestion; no authenticated HF API calls during training.
"""
import sys
from pathlib import Path

# Ensure ops can be imported
sys.path.insert(0, str(Path(__file__).parent))

from axentx.vanguard.ops.lightning_launcher import app

if __name__ == "__main__":
    # Running this directly will start the LightningApp.
    # For local-only testing without Lightning, you can import and run
    # SurrogateTrainer().run() after ensuring manifest exists.
    app.run()
```

---

## 5. Verification checklist

1. **Build manifest once**  
   ```bash
   cd /opt/axentx/vanguard
   HF_DATASET_REPO=datasets/axentx/surrogate-1 \
   HF_DATE_FOLDER=2026-04-29 \
   OUT_PATH=file-list.json \
   python scripts/discover_file_list.py
   ```
   Confirm `file-list.json` exists and contains only parquet paths for the folder.

2. **Run training (Lightning or local)**  
   ```bash
   cd /opt/axentx/vanguard
   python train.py
   ```
   - Logs should show CDN fetches (`resolve/main/...`) with no Authorization headers.
   - No
