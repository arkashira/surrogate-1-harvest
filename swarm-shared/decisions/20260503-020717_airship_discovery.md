# airship / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

**Goal (unchanged, highest value in <2h):**  
Eliminate HF API rate limits and Lightning quota waste during Surrogate training by implementing **CDN-first deterministic ingestion** + **Lightning Studio guard with reuse** + **idle-stop resilience** so training jobs survive studio timeouts and avoid redundant compute.

---

## Implementation Plan (≤2h)

### 1) Deterministic file-list snapshot (Mac orchestration)
- Single `list_repo_tree` per date folder → save `file_list.json`.
- Embed `file_list.json` in training script; Lightning training uses CDN URLs only (`resolve/main/...`) with zero API calls during data load.
- Avoid `load_dataset(streaming=True)` on mixed-schema repos; use `hf_hub_download` per file from CDN and project `{prompt, response}` at parse time.

### 2) Lightning Studio guard + reuse
- Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse any running studio with matching name.
- Wrap `.run()` with status check; if studio stopped, restart deterministically (`target.start(machine=Machine.L40S)`).
- Set `idle_timeout` handling: catch stop events and persist state so training can resume.

### 3) Surrogate/Arkship UI idle-stop resilience
- Expose lightweight `/training/status` and `/training/resume` endpoints.
- UI periodically pings status; on stopped/dead training, offers “Resume” which triggers guard logic above.
- Persist last file-list and training step to volume so resume is lossless.

### 4) Scripts to touch/create
- `scripts/discovery/collect_file_list.py` — deterministic snapshot.
- `scripts/training/lightning_guard.py` — studio reuse + idle-stop guard.
- `surrogate/api/training_routes.py` — status/resume endpoints.
- Update `surrogate/train.py` to accept file-list JSON and use CDN-only fetches.

---

## Code Snippets

### scripts/discovery/collect_file_list.py
```python
#!/usr/bin/env python3
"""
Collect deterministic file list for a date folder and emit file_list.json.
Run from Mac after HF API rate-limit window clears.
"""
import json
import os
import sys
from huggingface_hub import HfApi

REPO_ID = os.getenv("HF_DATASET_REPO", "org/surrogate-dataset")
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else "2026-04-29"
OUT_PATH = sys.argv[2] if len(sys.argv) > 2 else "file_list.json"

api = HfApi()
# non-recursive per folder to minimize pagination/rate-limit pressure
tree = api.list_repo_tree(repo_id=REPO_ID, path=DATE_FOLDER, recursive=False)

files = []
for item in tree:
    if item.type == "file":
        files.append({
            "path": item.path,
            "cdn_url": f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{item.path}"
        })

with open(OUT_PATH, "w") as f:
    json.dump({"repo_id": REPO_ID, "date_folder": DATE_FOLDER, "files": files}, f, indent=2)

print(f"Wrote {len(files)} files to {OUT_PATH}")
```

### scripts/training/lightning_guard.py
```bash
#!/usr/bin/env bash
# Guard script: reuse running Lightning Studio or restart if stopped.
# Usage: bash lightning_guard.py --name surrogate-l40s --machine L40S --script train.py --file-list file_list.json

set -euo pipefail
export SHELL=/bin/bash

NAME="surrogate-l40s"
MACHINE="L40S"
SCRIPT="train.py"
FILE_LIST="file_list.json"

while [[ $# -gt 0 ]]; do
  case $1 in
    --name) NAME="$2"; shift 2 ;;
    --machine) MACHINE="$2"; shift 2 ;;
    --script) SCRIPT="$2"; shift 2 ;;
    --file-list) FILE_LIST="$2"; shift 2 ;;
    *) shift ;;
  esac
done

python3 - <<PY
import os
import time
from lightning_sdk import Studio, Machine, Teamspace

NAME = os.getenv("NAME", "$NAME")
MACHINE = os.getenv("MACHINE", "$MACHINE")
SCRIPT = os.getenv("SCRIPT", "$SCRIPT")
FILE_LIST = os.getenv("FILE_LIST", "$FILE_LIST")

teamspace = Teamspace()
running = None
for s in teamspace.studios:
    if s.name == NAME and s.status == "running":
        running = s
        break

if running:
    print(f"Reusing running studio: {NAME}")
    studio = running
else:
    print(f"Creating studio: {NAME}")
    studio = Studio(
        name=NAME,
        machine=Machine(MACHINE),
        create_ok=True,
    )

# Ensure studio is running before run()
if studio.status != "running":
    print(f"Studio stopped; restarting on {MACHINE}")
    studio.start(machine=Machine(MACHINE))
    # wait briefly for running
    for _ in range(10):
        studio.refresh()
        if studio.status == "running":
            break
        time.sleep(6)

# Run training with file list (CDN-only mode)
run = studio.run(
    command=[
        "python", SCRIPT,
        "--file-list", FILE_LIST,
        "--use-cdn", "1"
    ],
    wait=False,
)
print(f"Started run {run.id}")
PY
```

### surrogate/api/training_routes.py
```python
from fastapi import APIRouter, HTTPException
from pathlib import Path
import json
import subprocess
import os

router = APIRouter()

STATE_FILE = Path("/data/surrogate/training_state.json")

def _load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

@router.get("/training/status")
def training_status():
    state = _load_state()
    return {
        "running": state.get("running", False),
        "last_file_list": state.get("last_file_list"),
        "last_step": state.get("last_step"),
        "studio": state.get("studio"),
    }

@router.post("/training/resume")
def resume_training(file_list: str = None):
    state = _load_state()
    file_list = file_list or state.get("last_file_list")
    if not file_list or not Path(file_list).exists():
        raise HTTPException(status_code=400, detail="file_list required and must exist")

    # Trigger guard script to reuse or restart studio
    script_dir = Path(__file__).parent.parent.parent / "scripts" / "training"
    guard = script_dir / "lightning_guard.py"
    if not guard.exists():
        raise HTTPException(status_code=404, detail="lightning_guard.py not found")

    cmd = [
        "bash", str(guard),
        "--name", "surrogate-l40s",
        "--machine", "L40S",
        "--script", "train.py",
        "--file-list", file_list,
    ]
    env = os.environ.copy()
    env.update({"NAME": "surrogate-l40s", "MACHINE": "L40S", "SCRIPT": "train.py", "FILE_LIST": file_list})

    try:
        proc = subprocess.Popen(cmd, env=env, cwd=script_dir.parent.parent)
        state.update({"running": True, "last_file_list": file_list, "pid": proc.pid})
        _save_state(state)
        return {"status": "resumed", "pid": proc.pid}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

### surrogate/train.py (CDN-only mode snippet)
```python
import argparse
import json
import requests
from pathlib import Path
from torch.utils.data import IterableDataset, DataLoader

class CDNIterableDataset(IterableDataset):
    def __init__(self, file_list_path, transform=None):
        super().__init__()
        with open(file_list_path) as f:
            meta = json.load(f)
        self.files = meta["files"]
        self.transform = transform

    def __iter
