# airship / discovery

### Final Implementation Plan (≤2h)  
**Goal:** Make Surrogate training HF-rate-limit-proof and Lightning-idle-resilient by embedding a CDN-only file list and adding auto-recovery for idle timeouts.  

---

### Why This Ships Value in <2h  
- **Eliminates HF API 429s** during training (bypass API, use CDN URLs).  
- **Survives Lightning idle stops** without manual intervention.  
- **No model changes**; only data-loading and lifecycle fixes.  

---

### Concrete Steps (90–120 min total)  

| Time | Task | Command / Code |
|------|------|----------------|
| 15m | Generate CDN file manifest (one-time) | `scripts/gen_cdn_manifest.sh` |
| 15m | Add `cdn_parquet_loader.py` (zero API calls) | See snippet below |
| 15m | Add Lightning auto-recovery wrapper | See snippet below |
| 15m | Wire into `train.py` entrypoint | See snippet below |
| 30m | Test locally (dry-run) + verify no HF API calls | `grep -r "huggingface.co/api" .` |
| 15m | Commit and update runbook | Brief notes |

---

### 1) Pre-list CDN File Manifest (run once)  

```bash
#!/usr/bin/env bash
# surrogate/scripts/gen_cdn_manifest.sh
# Usage: HF_REPO="datasets/your/repo" DATE_DIR="batches/mirror-merged/2026-05-03" ./gen_cdn_manifest.sh

set -euo pipefail
export HF_REPO="${HF_REPO:-datasets/your/repo}"
export DATE_DIR="${DATE_DIR:-batches/mirror-merged/$(date +%Y-%m-%d)}"
export OUT="${OUT:-surrogate/data/filelist.json}"

python3 - "$HF_REPO" "$DATE_DIR" "$OUT" <<'PY'
import os, json, sys
from huggingface_hub import list_repo_tree

repo = sys.argv[1]
path = sys.argv[2]
out  = sys.argv[3]

# Single non-recursive call to avoid pagination/429
tree = list_repo_tree(repo_id=repo, path=path, recursive=False)
files = [
    f"{path.rstrip('/')}/{node.path}"
    for node in tree
    if node.type == "file" and node.path.lower().endswith(".parquet")
]

cdn_urls = [
    f"https://huggingface.co/datasets/{repo}/resolve/main/{f}"
    for f in files
]

os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump({"repo": repo, "date_dir": path, "files": files, "cdn_urls": cdn_urls}, f, indent=2)

print(f"Wrote {len(cdn_urls)} files to {out}")
PY
```

- **Embed `filelist.json`** in the repo or bake into the Docker image so training uses CDN-only fetches.  

---

### 2) CDN-Only Data Loader (Zero HF API Calls During Training)  

```python
# surrogate/data/cdn_parquet_loader.py
import json
import pyarrow.parquet as pq
import requests
import io
from typing import List, Dict, Any
from datasets import Dataset

def load_cdn_parquet_shard(cdn_url: str, columns=("prompt", "response")) -> List[Dict[str, Any]]:
    """Download one Parquet file via CDN (no HF API/auth)."""
    resp = requests.get(cdn_url, timeout=30)
    resp.raise_for_status()
    table = pq.read_table(io.BytesIO(resp.content), columns=columns)
    return table.to_pylist()

def build_dataset_from_manifest(manifest_path: str) -> Dataset:
    """Build HF Dataset from CDN filelist (zero API calls)."""
    with open(manifest_path) as f:
        manifest = json.load(f)

    rows = []
    for url in manifest["cdn_urls"]:
        try:
            rows.extend(load_cdn_parquet_shard(url))
        except Exception as exc:
            print(f"Skipping {url}: {exc}")
            continue

    return Dataset.from_list(rows)
```

---

### 3) Lightning Auto-Recovery Wrapper  

```python
# surrogate/train/lightning_recovery.py
import time
from lightning import Lightning, Teamspace, Machine, Studio

def get_or_create_studio(name: str, machine: Machine = Machine.L40S) -> Studio:
    """Reuse running studio; restart if idle-stopped."""
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name:
            if s.status == "running":
                print(f"Reusing running studio: {name}")
                return s
            else:
                print(f"Studio {name} stopped ({s.status}). Restarting...")
                s.start(machine=machine)
                return s

    print(f"Creating studio: {name}")
    return Studio(name=name, machine=machine, create_ok=True)

def run_with_recovery(train_fn, studio_name: str = "surrogate-train", max_retries: int = 3):
    """Run training with idle-stop recovery."""
    for attempt in range(1, max_retries + 1):
        try:
            studio = get_or_create_studio(studio_name)
            studio.run(train_fn)
            return
        except Exception as exc:
            print(f"Attempt {attempt}/{max_retries} failed: {exc}")
            if attempt == max_retries:
                raise
            wait = 60 * attempt
            print(f"Waiting {wait}s before retry...")
            time.sleep(wait)
```

---

### 4) Wire Into Entrypoint  

```python
# surrogate/train.py
import argparse
from surrogate.data.cdn_parquet_loader import build_dataset_from_manifest
from surrogate.train.lightning_recovery import run_with_recovery

def train_job(manifest_path: str):
    dataset = build_dataset_from_manifest(manifest_path)
    print(f"Loaded {len(dataset)} examples from CDN")
    # Your existing training loop here (model, dataloader, optimizer, etc.)
    # ...

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="surrogate/data/filelist.json")
    args = parser.parse_args()

    run_with_recovery(lambda: train_job(args.manifest), studio_name="surrogate-train")
```

---

### Test and Verify  

```bash
# Dry-run locally (no Lightning)
python surrogate/train.py --manifest surrogate/data/filelist.json

# Verify no HF API calls during training
grep -r "huggingface.co/api" .
# Should return nothing from training code
```

---

### Commit and Runbook Notes  
- Add `filelist.json` to repo (or bake into image).  
- Update runbook:  
  - “If training stalls due to idle timeout, Lightning auto-recovery will restart the studio and resume.”  
  - “Training uses CDN-only URLs; HF API is only used once (offline) to generate the manifest.”
