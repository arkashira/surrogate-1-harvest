# airship / discovery

## Highest-Value Incremental Improvement (<2h)
**Deterministic CDN-only data loading + Lightning Studio lifecycle resilience** for Surrogate-1 training.  
Eliminates HF API 429s during data loads and prevents quota loss from idle-stop/studio recreation.

---

## Implementation Plan

### 1. Pre-list file paths once (Mac orchestration)
- Single API call to `list_repo_tree(path, recursive=False)` for one date folder.
- Save list to `file_list.json`.
- Embed in `train.py`.

### 2. CDN-only fetches during training (Lightning)
- Use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with no Authorization header.
- Avoid `load_dataset(streaming=True)` for heterogeneous repos.
- Download each file via CDN, project to `{prompt, response}` at parse time.

### 3. Lightning Studio reuse + idle-resilience
- Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse running ones.
- Check status before each `.run()`; restart with `target.start(machine=Machine.L40S)` if stopped.

---

## Code Snippets

### 1) Pre-list and save file list (run on Mac)
```bash
# list_files.py
#!/usr/bin/env python3
import json
from huggingface_hub import HfApi

api = HfApi()
repo_id = "your-org/surrogate-1-data"
folder = "batches/mirror-merged/2026-05-03"

tree = api.list_repo_tree(repo_id=repo_id, path=folder, recursive=False)
files = [item.path for item in tree if item.type == "file"]

with open("file_list.json", "w") as f:
    json.dump(files, f, indent=2)

print(f"Saved {len(files)} files to file_list.json")
```

### 2) CDN-only dataset loader (in Lightning training script)
```python
# train.py
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from pathlib import Path

REPO = "your-org/surrogate-1-data"
BASE_URL = f"https://huggingface.co/datasets/{REPO}/resolve/main"

def load_file_cdn(path: str):
    url = f"{BASE_URL}/{path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return pq.read_table(BytesIO(resp.content))

def build_dataset(file_list_path="file_list.json"):
    with open(file_list_path) as f:
        files = json.load(f)

    rows = []
    for file in files:
        table = load_file_cdn(file)
        # Project to {prompt, response} only
        for batch in table.to_batches():
            cols = batch.column_names
            if "prompt" in cols and "response" in cols:
                for i in range(batch.num_rows):
                    rows.append({
                        "prompt": batch["prompt"][i].as_py(),
                        "response": batch["response"][i].as_py(),
                    })
    return rows
```

### 3) Lightning Studio reuse + idle-resilience
```python
# lightning_launcher.py
import time
from lightning import Lightning, L40S, Teamspace

def get_or_create_studio(name="surrogate-1-train"):
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s

    print("Creating new studio...")
    return Lightning.Studio(
        name=name,
        machine=L40S,
        create_ok=True,
    )

def run_with_retry(studio, script, args, max_retries=3):
    for attempt in range(max_retries):
        if studio.status != "Running":
            print(f"Studio stopped (attempt {attempt+1}). Restarting...")
            studio.start(machine=L40S)
            time.sleep(30)  # wait for startup

        try:
            job = studio.run(script, args)
            return job
        except Exception as e:
            print(f"Run failed: {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(60)

if __name__ == "__main__":
    studio = get_or_create_studio()
    run_with_retry(studio, "train.py", ["--epochs", "3"])
```

---

## Execution Steps (≤2h)
1. Run `python list_files.py` on Mac (after HF rate-limit window clears) → produces `file_list.json`.
2. Commit `file_list.json` + updated `train.py` + `lightning_launcher.py`.
3. Launch via `python lightning_launcher.py` (or schedule in cron with `SHELL=/bin/bash`).
4. Monitor: training uses CDN-only fetches (zero API calls during data load) and reuses running studio.
