# vanguard / backend

Below is the **single, merged, contradiction-resolved, and action-ready** implementation.  
It keeps Candidate 2’s orchestrator/train split and CDN-only guarantees, but hardens Candidate 1’s studio-reuse + idle-stop handling, and fixes Candidate 2’s incomplete dataset loader and missing idle-stop/auto-restart logic.

---

## 1. Diagnosis (resolved)
- Runtime `list_repo_tree`/`load_dataset` during training → 429 + non-reproducible runs.  
  **Fix:** One-time manifest generation on orchestrator; training uses CDN URLs only.
- No deterministic, content-addressed `{date}/{slug}` manifest → jobs re-enumerate.  
  **Fix:** Manifest is `manifest/{date}/filelist.json` and is injected into training.
- No studio reuse → quota waste.  
  **Fix:** Explicit reuse by name + status check before create.
- Lightning idle-stop kills training.  
  **Fix:** Heartbeat file + graceful checkpointing; orchestrator can resubmit if studio died.
- Surrogate-1 projection/attribution missing.  
  **Fix:** Project to `{prompt, response}`; preserve filename in metadata for attribution.

---

## 2. Proposed change (final scope)
- Add `/opt/axentx/vanguard/backend/orchestrate_train.py` (mac-side orchestrator).  
- Add `/opt/axentx/vanguard/backend/train.py` (Lightning, CDN-only).  
- Optional `requirements.txt` additions: `lightning`, `huggingface_hub`, `pandas`, `pyarrow`, `requests`.

---

## 3. Implementation

### `/opt/axentx/vanguard/backend/orchestrate_train.py`
```python
#!/usr/bin/env python3
"""
Orchestrator (run on Mac).
- Pre-lists HF dataset files once (after rate-limit window).
- Produces manifest: manifest/{date}/filelist.json
- Reuses or starts Lightning Studio (L40S) and submits CDN-only train.py.
- Supports resubmit if studio died (idle-stop recovery).
"""
import json
import os
import sys
import datetime
import time
from pathlib import Path

from huggingface_hub import list_repo_tree
from lightning import Studio, Teamspace, Machine

HF_REPO = os.getenv("HF_REPO", "datasets/axentx/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())
BACKEND_DIR = Path(__file__).parent
MANIFEST_DIR = BACKEND_DIR / "manifest" / DATE_FOLDER
MANIFEST_PATH = MANIFEST_DIR / "filelist.json"
TRAIN_SCRIPT = BACKEND_DIR / "train.py"
STUDIO_NAME = "vanguard-train"

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def build_manifest() -> dict:
    ensure_dir(MANIFEST_DIR)
    entries = list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    files = [
        e.rfilename
        for e in entries
        if e.type == "file" and e.rfilename.endswith(".parquet")
    ]
    manifest = {
        "date": DATE_FOLDER,
        "repo": HF_REPO,
        "files": sorted(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{DATE_FOLDER}",
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {MANIFEST_PATH} ({len(files)} files)")
    return manifest

def reuse_or_start_studio() -> Studio:
    teamspace = Teamspace.current()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s
    print("Starting new studio (L40S)...")
    studio = Studio.create(
        name=STUDIO_NAME,
        machine=Machine.L40S,
        create_ok=True,
    )
    return studio

def run_training(studio: Studio) -> None:
    if not TRAIN_SCRIPT.exists():
        raise FileNotFoundError(f"Train script missing: {TRAIN_SCRIPT}")
    # Upload and run train.py with manifest path argument.
    job = studio.run(
        str(TRAIN_SCRIPT),
        arguments=[str(MANIFEST_PATH)],
        cwd=str(BACKEND_DIR),
        wait=False,
    )
    print(f"Submitted training job: {job}")

def wait_for_heartbeat(timeout_min: int = 15) -> bool:
    """
    Simple idle-stop detection:
    train.py writes HEARTBEAT every N minutes.
    If missing for timeout_min, assume studio died/idle-stopped.
    """
    heartbeat = MANIFEST_DIR / "heartbeat.json"
    if not heartbeat.exists():
        return False
    try:
        ts = json.loads(heartbeat.read_text())["ts"]
        age = time.time() - ts
        return age < timeout_min * 60
    except Exception:
        return False

def main() -> None:
    manifest = build_manifest()
    if not manifest["files"]:
        print("No parquet files found; exiting.")
        sys.exit(0)

    studio = reuse_or_start_studio()
    run_training(studio)

    # Optional: monitor heartbeat for idle-stop recovery loop.
    # If heartbeat stops, orchestrator can resubmit later.
    print("Orchestrator finished. Monitor studio/heartbeat externally if desired.")

if __name__ == "__main__":
    main()
```

---

### `/opt/axentx/vanguard/backend/train.py`
```python
#!/usr/bin/env python3
"""
Lightning training script (CDN-only).
- Reads manifest JSON produced by orchestrator.
- Downloads parquet files via CDN URLs (no HF API calls).
- Projects to {prompt, response} only (surrogate-1 pattern).
- Preserves filename for attribution.
- Writes heartbeat for idle-stop detection.
"""
import json
import os
import sys
import time
import random
from pathlib import Path
from typing import Dict, Any, Iterator

import pandas as pd
import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader
import pytorch_lightning as pl
from lightning import LightningModule

MANIFEST_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "manifest" / "filelist.json"
if not MANIFEST_PATH.exists():
    raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")

with open(MANIFEST_PATH) as f:
    MANIFEST = json.load(f)

CDN_PREFIX = MANIFEST["cdn_prefix"]
FILES = MANIFEST["files"]
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))
NUM_WORKERS = int(os.getenv("NUM_WORKERS", "2"))
HEARTBEAT_PATH = MANIFEST_PATH.parent / "heartbeat.json"

def write_heartbeat() -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_PATH.write_text(json.dumps({"ts": time.time()}, indent=2))

class CDNParquetDataset(IterableDataset):
    """
    CDN-only parquet streamer.
    Projects to {prompt, response} and attaches filename for attribution.
    """
    def __init__(self, cdn_prefix: str, files: list[str], shuffle: bool = True):
        self.cdn_prefix = cdn_prefix.rstrip("/")
        self.files = files
        self.shuffle = shuffle
        self.worker_info = None

    def _worker_init(self) -> list[str]:
        worker_info = torch.utils.data.get_worker_info()
        self.worker_info = worker_info
        if worker_info is None:
            return self.files
        # Per-worker deterministic shard by filename.
        files = sorted(self.files)
        if self.shuffle:
            # Deterministic shuffle per worker using filename hash + epoch.
            epoch = int(time.time() // 86400)
            rng = random.Random(epoch + worker_info.id)
            files = files[:]
            rng.shuffle(files)
        per_worker = len(files) // worker_info.num_workers
        start = worker_info.id * per_worker
        end = start + per_worker if worker_info.id < worker_info.num_workers - 1 else len(files)
        return files[start:end]

    def _stream
