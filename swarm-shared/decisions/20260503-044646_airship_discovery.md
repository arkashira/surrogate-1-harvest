# airship / discovery

## Final Synthesized Implementation (Highest-Value, ≤2h)

**Resilient Surrogate-1 Training Pipeline**  
Eliminate HF API 429s and Lightning idle-stop quota waste by switching to deterministic CDN-only data loading + studio lifecycle resilience.

---

## Implementation Plan

### 1. Pre-list file paths once (Mac orchestration)
- **Single API call** to `list_repo_tree` for the target date folder (non-recursive).
- Save `file_list.json` into repo (e.g., `batches/mirror-merged/2026-05-03/file_list.json`).
- **Embed this list in `train.py`**; Lightning training will use CDN URLs only (zero API calls during data load).

### 2. CDN-only data loader (deterministic, no auth)
- **Replace `load_dataset(streaming=True)`** with direct CDN GET requests for each file in the pre-listed set.
- Parse each file to `{prompt, response}` only at parse time; drop extra schema columns.
- **Use deterministic repo selection for writes**: `repo = f"axentx/surrogate-mirror-shard-{hash(slug) % 5}"` to spread HF commit cap (128/hr).

### 3. Lightning Studio lifecycle resilience
- **Before `.run()`**, check `Teamspace.studios` for existing running studio with matching name; reuse if found.
- If studio is stopped, restart with `target.start(machine=Machine.L40S)` (fallback to public tier if quota exhausted).
- **Wrap training script start in a loop** that checks status and restarts on idle-stop.

### 4. Commit & push artifacts deterministically
- Save `file_list.json` and a small `train_config.json` (date, repo list, machine type) to repo.
- Use sibling repo hashing to avoid HF commit cap during ingestion writes.

---

## Code Snippets

### 1.1 Pre-list file paths (run once on Mac)
```bash
# list files for a specific date folder (non-recursive)
python -c "
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_tree(
    repo_id='axentx/surrogate-mirror',
    path='batches/mirror-merged/2026-05-03',
    recursive=False
)
import json
with open('file_list.json','w') as f:
    json.dump([f.rfilename for f in files if f.rfilename.endswith('.parquet')], f)
"
```

### 1.2 CDN-only dataset loader (train.py)
```python
import json
import os
import pyarrow.parquet as pq
import requests
from io import BytesIO
from pathlib import Path

REPO = "axentx/surrogate-mirror"
BASE_URL = "https://huggingface.co/datasets"

def load_shard_cdn(file_path: str):
    """Download single parquet via CDN (no auth) and return prompt/response pairs."""
    url = f"{BASE_URL}/{REPO}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    table = pq.read_table(BytesIO(resp.content))
    # Project only required columns; tolerate extra schema
    cols = set(table.column_names)
    prompt_col = next((c for c in ["prompt", "instruction", "input"] if c in cols), None)
    response_col = next((c for c in ["response", "output", "completion"] if c in cols), None)
    if not prompt_col or not response_col:
        raise ValueError(f"Missing prompt/response in {file_path}")
    return [
        {"prompt": str(row[prompt_col]), "response": str(row[response_col])}
        for row in table.to_pylist()
    ]

def build_dataset(file_list_path="file_list.json"):
    with open(file_list_path) as f:
        files = json.load(f)
    examples = []
    for fpath in files:
        examples.extend(load_shard_cdn(fpath))
    return examples

if __name__ == "__main__":
    data = build_dataset()
    print(f"Loaded {len(data)} examples via CDN")
```

### 1.3 Lightning Studio lifecycle resilience (launcher.py)
```python
from lightning_sdk import Studio, Machine, Teamspace
import time

TEAMSPACE = "axentx"
STUDIO_NAME = "surrogate-train-l40s"

def ensure_studio_running():
    teamspace = Teamspace(name=TEAMSPACE)
    running = None
    for s in teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "running":
            running = s
            break
    if running:
        print(f"Reusing running studio: {STUDIO_NAME}")
        return running
    # start new or stopped studio
    target = Studio(
        name=STUDIO_NAME,
        machine=Machine.L40S,
        teamspace=TEAMSPACE,
        create_ok=True,
    )
    if target.status == "stopped":
        print("Restarting stopped studio...")
        target.start(machine=Machine.L40S)
    else:
        print("Starting new studio...")
        target.start(machine=Machine.L40S)
    # wait for running
    while target.status != "running":
        time.sleep(10)
        target.refresh()
    print("Studio is running")
    return target

def run_training(script_path="train.py"):
    studio = ensure_studio_running()
    run = studio.run(
        command=["python", script_path],
        environment="base",
    )
    print(f"Run submitted: {run.id}")
    return run

if __name__ == "__main__":
    run_training()
```

### 1.4 Deterministic sibling repo selection (optional)
```python
import hashlib

def pick_sibling_repo(slug: str, siblings=5):
    """Deterministically pick one of N sibling repos to spread HF commit load."""
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % siblings
    return f"axentx/surrogate-mirror-shard-{idx}"
```

---

## Acceptance Criteria
- Training script loads data **exclusively via CDN URLs** (no `load_dataset` or HF API data calls).
- Launcher checks studio status and restarts if stopped before submitting runs.
- Pre-generated `file_list.json` committed to repo (or passed via env) to avoid recursive HF API calls during training.
- No HF API 429s observed during data loading; Lightning quota preserved via studio reuse.
