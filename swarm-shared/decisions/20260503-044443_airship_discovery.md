# airship / discovery

Candidate 3:
## Highest-Value Incremental Improvement (<2h)

**Goal:** Make Surrogate-1 training resilient to HF API 429s and Lightning Studio idle-stop kills by implementing deterministic CDN-only data loading + Studio lifecycle reuse.

**Why:**  
- Eliminates HF API rate-limit failures during data loading (uses CDN direct URLs).  
- Prevents quota loss from idle-stop/studio recreation (reuse running studios).  
- Fits in <2h: single script + small training loader change.

---

## Implementation Plan

### 1) Pre-list file paths once (Mac orchestration)
- Run `list_repo_tree` for the target date folder (non-recursive) and save to `file_list.json`.
- Embed `file_list.json` in the training repo so Lightning training uses CDN-only fetches (zero API calls during data load).

### 2) CDN-only data loader
- Replace `load_dataset(streaming=True)` with per-file `hf_hub_download` (or raw CDN URLs) and project to `{prompt, response}` at parse time.
- Use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` for public files (no auth, bypasses API rate limit).

### 3) Lightning Studio reuse + idle resilience
- Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse a running studio with matching name.
- Wrap each `.run()` with status check; if studio stopped, restart with `target.start(machine=Machine.L40S)`.

### 4) Schema hygiene
- Project to `{prompt, response}` only before upload.
- Save attribution in filename pattern: `batches/mirror-merged/{date}/{slug}.parquet` (no extra `source`/`ts` columns).

---

## Code Snippets

### 1) Pre-list file paths (run on Mac)
```bash
#!/usr/bin/env bash
# scripts/list_cdn_files.sh
set -euo pipeoto

REPO="datasets/your-repo"
DATE_FOLDER="2026-05-03"
OUT="file_list.json"

python3 - <<PY
import json, os
from huggingface_hub import HfApi

api = HfApi()
files = api.list_repo_tree(
    repo_id=os.environ.get("HF_REPO", "$REPO"),
    path="$DATE_FOLDER",
    recursive=False,
    repo_type="dataset"
)
# Keep only files we want to train on
file_list = [f.rfilename for f in files if f.rfilename.endswith(('.jsonl', '.parquet', '.json'))]
with open("$OUT", "w") as f:
    json.dump(file_list, f, indent=2)
print(f"Saved {len(file_list)} files to $OUT")
PY
```

### 2) CDN-only data loader (training script)
```python
# surrogate/train.py
import json
import pyarrow.parquet as pq
import requests
from pathlib import Path
from torch.utils.data import IterableDataset

HF_DATASET = "datasets/your-repo"
CDN_BASE = f"https://huggingface.co/{HF_DATASET}/resolve/main"

class CDNParquetDataset(IterableDataset):
    def __init__(self, file_list_path):
        with open(file_list_path) as f:
            self.files = json.load(f)
        self.worker_info = None
    
    def __iter__(self):
        worker_id = 0
        if self.worker_info is not None:
            worker_id = self.worker_info.id
        
        # Simple round-robin across workers
        for i, fname in enumerate(self.files):
            if i % self.num_workers != worker_id:
                continue
            
            # CDN fetch - no Authorization header
            url = f"{CDN_BASE}/{fname}"
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            
            # Parse only {prompt, response}
            table = pq.read_table(pq.ParquetFile(resp.content))
            for row in table.to_pylist():
                yield {
                    "prompt": row.get("prompt", ""),
                    "response": row.get("response", "")
                }
    
    def set_worker_info(self, worker_info):
        self.worker_info = worker_info
        self.num_workers = worker_info.num_workers if worker_info else 1
```

### 3) Lightning Studio reuse + idle resilience
```python
# surrogate/lightning_launcher.py
from lightning import Lightning, Teamspace, Machine
import time

def get_or_create_studio(studio_name: str):
    lightning = Lightning()
    teamspace = Teamspace()
    
    # Reuse running studio
    for s in teamspace.studios:
        if s.name == studio_name and s.status == 'Running':
            print(f"Reusing running studio: {studio_name}")
            return s
    
    # Create new if not exists
    for s in teamspace.studios:
        if s.name == studio_name:
            print(f"Restarting stopped studio: {studio_name}")
            s.start(machine=Machine.L40S)
            return s
    
    print(f"Creating new studio: {studio_name}")
    return lightning.studio.create(
        name=studio_name,
        machine=Machine.L40S,
        create_ok=True
    )

def run_training_with_retry(studio_name, script_path, max_retries=3):
    for attempt in range(max_retries):
        try:
            studio = get_or_create_studio(studio_name)
            
            # Check status before run
            if studio.status != 'Running':
                studio.start(machine=Machine.L40S)
                time.sleep(30)  # Wait for startup
            
            # Run training
            studio.run(script_path)
            return
            
        except Exception as e:
            print(f"Attempt {attempt+1} failed: {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(60)
```

### 4) Integration entrypoint
```python
# surrogate/run_training.py
if __name__ == "__main__":
    # Phase 1: Generate file list (once per date folder)
    # bash scripts/list_cdn_files.sh
    
    # Phase 2: Run training with CDN + lifecycle guard
    run_training_with_retry(
        studio_name="surrogate-1-training",
        script_path="train.py"
    )
```

---

## Verification Steps

1. **Zero API calls during data load:**
   ```bash
   # Monitor HF API calls (should be 0 during training)
   grep -c "huggingface.co/api" lightning_logs/*.log
   ```

2. **Studio reuse:**
   ```bash
   # Should show "Reusing running studio" on second run
   python surrogate/run_training.py
   ```

3. **CDN fetch test:**
   ```python
   # Quick test - should work without HF token
   import requests
   r = requests.get("https://huggingface.co/datasets/your-repo/resolve/main/2026-04-29/file.parquet")
   assert r.status_code == 200
   ```

**Time estimate:** 85 minutes total (including testing).
