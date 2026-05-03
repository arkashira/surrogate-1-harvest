# airship / discovery

**Final consolidated plan (highest-value, rate-limit-proof, idle-resilient)**

**Goal**: Make Surrogate training HF-rate-limit-proof and Lightning-idle-resilient in <2 h with concrete, copy-pasteable artifacts.

---

### 1) One-time CDN-only manifest (Mac)
- **Why**: Bypasses `/api/` auth and rate limits during training; no HF client calls while training.
- **Action**:
  1. On the Mac, run a **single** `list_repo_tree(..., recursive=False)` for the target date folder.
  2. Emit `manifest.json` with CDN URLs (`resolve/main/...`).
  3. Commit `manifest.json` (or copy it) to the training environment.

**`scripts/build_manifest.py`**
```python
#!/usr/bin/env python3
"""
Generate CDN-only manifest for Surrogate training.
Run on Mac after any HF API rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("pip install huggingface_hub")
    sys.exit(1)

REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUT_PATH = Path("manifest.json")

api = HfApi()

def build_manifest() -> None:
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        cdn_url = (
            f"https://huggingface.co/datasets/{REPO_ID}"
            f"/resolve/main/{DATE_FOLDER}/{entry.path}"
        )
        files.append(
            {
                "path": entry.path,
                "cdn_url": cdn_url,
                "size": getattr(entry, "size", None),
            }
        )

    manifest = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    OUT_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files to {OUT_PATH}")

if __name__ == "__main__":
    build_manifest()
```

Run:
```bash
python scripts/build_manifest.py
# copy manifest.json into training container/environment
```

---

### 2) CDN-only data loader in `train.py`
- **Why**: Zero HF API calls during training; resilient to rate limits and token expiry.
- **Action**:
  - Read `manifest.json`.
  - Fetch `.parquet` bytes via `requests.get(cdn_url)` with retries.
  - Parse only required columns (`prompt`, `response`) to minimize memory.

**`train.py` (excerpt)**
```python
import json
import io
import time
from pathlib import Path
from typing import Dict, List

import requests
import pyarrow.parquet as pq

MANIFEST_PATH = Path("manifest.json")

def load_manifest() -> Dict:
    return json.loads(MANIFEST_PATH.read_text())

def fetch_parquet_bytes(cdn_url: str, retries: int = 3, backoff: float = 2.0) -> bytes:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(cdn_url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == retries:
                raise
            time.sleep(backoff ** attempt)

def load_dataset_from_manifest(manifest: Dict) -> List[Dict]:
    records = []
    for item in manifest["files"]:
        if not item["path"].endswith(".parquet"):
            continue
        raw = fetch_parquet_bytes(item["cdn_url"])
        table = pq.read_table(io.BytesIO(raw))
        # Project early to reduce memory
        df = table.select(["prompt", "response"]).to_pandas()
        for _, row in df.iterrows():
            records.append({"prompt": row["prompt"], "response": row["response"]})
    return records

if __name__ == "__main__":
    manifest = load_manifest()
    dataset = load_dataset_from_manifest(manifest)
    print(f"Loaded {len(dataset)} examples via CDN")
    # Continue training...
```

---

### 3) Idle-resilient Lightning launcher
- **Why**: Lightning Studio stops on idle and can kill long runs. Auto-restart + re-run ensures progress.
- **Action**:
  - Before each training run, check studio status.
  - If stopped, restart on `L40S`.
  - Run training; on completion or failure, optionally stop studio to avoid idle billing.

**`lightning_launcher.py`**
```python
#!/usr/bin/env python3
"""
Lightning Studio launcher with idle-stop resilience.
Checks status and restarts studio if stopped before run.
"""
import time
import sys
from pathlib import Path

try:
    from lightning_sdk import Studio, Teamspace, Machine
except ImportError:
    print("pip install lightning")
    sys.exit(1)

STUDIO_NAME = "surrogate-train-l40s"
MACHINE = Machine.L40S
MAX_RETRIES = 3

def get_or_create_studio() -> Studio:
    teamspace = Teamspace()
    running = [s for s in teamspace.studios if s.name == STUDIO_NAME and s.status == "running"]
    if running:
        print(f"Reusing running studio: {STUDIO_NAME}")
        return running[0]

    existing = [s for s in teamspace.studios if s.name == STUDIO_NAME]
    if existing:
        studio = existing[0]
        if studio.status == "stopped":
            print(f"Starting stopped studio: {STUDIO_NAME}")
            studio.start(machine=MACHINE)
            return studio
        # For other states (e.g., busy), wait briefly and re-check
        return studio

    print(f"Creating new studio: {STUDIO_NAME}")
    return Studio(
        name=STUDIO_NAME,
        project="surrogate-training",
    )

def run_training_with_resilience() -> None:
    manifest_path = Path("manifest.json")
    if not manifest_path.exists():
        print("ERROR: manifest.json not found. Run build_manifest.py first.")
        sys.exit(1)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            studio = get_or_create_studio()
            if studio.status != "running":
                print(f"Studio not running (status={studio.status}). Starting...")
                studio.start(machine=MACHINE)

            print(f"Running training on {STUDIO_NAME} (attempt {attempt})")
            run = studio.run(
                # Adjust entrypoint and requirements to your setup
                entrypoint="python train.py",
                requirements=["torch", "pyarrow", "pandas", "requests"],
                machine=MACHINE,
            )
            # Block until run completes
            run_url = run.url
            print(f"Run started: {run_url}")
            run.wait_until_finished()
            print("Training finished.")
            return
        except Exception as exc:
            print(f"Attempt {attempt} failed: {exc}")
            if attempt == MAX_RETRIES:
                print("All attempts exhausted.")
                raise
            time.sleep(10)

if __name__ == "__main__":
    run_training_with_resilience()
```

Make executable:
```bash
chmod +x lightning_launcher.py
```

---

### 4) Cron-safe wrapper (optional but recommended)
Ensures launcher is invoked reliably from cron or CI.

**`run_training.sh`**
```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root
source .venv/bin/activate  # or your env setup

exec python lightning_launcher.py
```
```bash
chmod +x run_training.sh
```

Cron example (every 6 hours):
```cron
0 */6 * * * /path/to/run_training.sh >> /var/log/surrogate_training.log 2>&1
```


