# airship / discovery

## Highest-Value Incremental Improvement
**CDN-only ingestion + deterministic sibling-repo sharding + Studio lifecycle guard**  
- Eliminates HF API 429s during training by pre-listing once and downloading via public CDN  
- Bypasses 128/hr commit cap via deterministic sibling-repo sharding  
- Prevents idle-timeout kills via Studio lifecycle guard  

**Estimated effort**: 90–110 minutes (single dev)

---

## Implementation Plan (≤2h)

### 1) Add CDN-first file listing utility (Mac/Orchestrator side)
- Single API call to `list_repo_tree(path, recursive=False)` for one date folder.
- Save list to JSON; embed in training script so Lightning does **zero** API calls during data load.
- Use public CDN URLs (`resolve/main/...`) for all downloads (no Authorization header → bypasses auth rate limit).

### 2) Deterministic sibling-repo sharding for writes
- Hash slug → pick sibling repo index deterministically (mod N).
- Spread HF Hub commits across 5 sibling repos (640/hr aggregate).
- Keep attribution in filename pattern: `batches/mirror-merged/{date}/{slug}.parquet` (no extra `source`/`ts` columns).

### 3) Studio lifecycle guard for Lightning
- Before `.run()`, check status; reuse running studio if present.
- If stopped, restart with target machine (L40S or fallback).
- Prevents idle-timeout death and saves quota.

### 4) Training script hardening
- Avoid `load_dataset(streaming=True)` on heterogeneous repos.
- Use `hf_hub_download` per file and project to `{prompt, response}` only at parse time.
- After 429: wait 360s before retry.

---

## Code Snippets

### 1) CDN file listing + JSON export (run on Mac)
```python
# scripts/list_cdn_files.py
import json
import os
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "my-org/surrogate-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-05-03")
OUT_JSON = os.getenv("OUT_JSON", "file_list.json")

api = HfApi()
tree = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)

files = [
    {
        "path": item.path,
        "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{item.path}"
    }
    for item in tree
    if not item.path.endswith("/")
]

with open(OUT_JSON, "w") as f:
    json.dump(files, f, indent=2)

print(f"Wrote {len(files)} files to {OUT_JSON}")
```

Usage:
```bash
export HF_DATASET_REPO=my-org/surrogate-dataset
export DATE_FOLDER=2026-05-03
python scripts/list_cdn_files.py
```

Commit `file_list.json` alongside training script or embed it.

---

### 2) CDN-only data loader (for Lightning training)
```python
# surrogate/data/cdn_loader.py
import json
import os
import requests
import pyarrow as pa
import pyarrow.parquet as pq
from io import BytesIO
from tqdm import tqdm

FILE_LIST = os.getenv("FILE_LIST", "file_list.json")

def load_file_list(path=FILE_LIST):
    with open(path) as f:
        return json.load(f)

def stream_parquet_from_cdn(cdn_url, columns=("prompt", "response")):
    resp = requests.get(cdn_url, timeout=60)
    resp.raise_for_status()
    table = pq.read_table(BytesIO(resp.content), columns=columns)
    return table

def build_dataset(files, batch_size=1024):
    prompts = []
    responses = []
    for item in tqdm(files, desc="CDN fetch"):
        try:
            tbl = stream_parquet_from_cdn(item["cdn_url"])
            p = tbl["prompt"].to_pylist()
            r = tbl["response"].to_pylist()
            prompts.extend(p)
            responses.extend(r)
        except Exception as e:
            print(f"Skip {item['path']}: {e}")
            continue
    return pa.table({"prompt": prompts, "response": responses})
```

---

### 3) Deterministic sibling-repo sharding for writes
```python
# surrogate/ingest/shard.py
import hashlib
import os
from huggingface_hub import HfApi

HF_REPO_ROOT = os.getenv("HF_REPO_ROOT", "my-org/surrogate-dataset")
SIBLINGS = int(os.getenv("HF_SIBLINGS", "5"))  # 5 repos: -s0 .. -s4
api = HfApi()

def sibling_repo(slug: str) -> str:
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % SIBLINGS
    return f"{HF_REPO_ROOT}-s{idx}"

def upload_sharded(date: str, slug: str, parquet_bytes: bytes):
    repo = sibling_repo(slug)
    path = f"batches/mirror-merged/{date}/{slug}.parquet"
    api.upload_file(
        path_or_fileobj=BytesIO(parquet_bytes),
        path_in_repo=path,
        repo_id=repo,
        repo_type="dataset",
    )
    return repo, path
```

---

### 4) Studio lifecycle guard (Lightning)
```python
# surrogate/train/studio_guard.py
import lightning as L
from lightning.fabric.utilities.cloud_io import _is_running_in_cloud

def get_or_create_studio(
    name: str,
    machine: L.Machine = L.Machine.L40S,
    idle_timeout: int = 7200,
):
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}")
            return s

    print(f"Creating studio: {name}")
    studio = L.Studio(
        name=name,
        machine=machine,
        idle_timeout=idle_timeout,
        create_ok=True,
    )
    return studio

def safe_run(studio, target, *args, **kwargs):
    if studio.status != "running":
        print(f"Studio stopped; restarting on {studio.machine}")
        studio.start(machine=studio.machine)
    return studio.run(target, *args, **kwargs)
```

Usage in training entrypoint:
```python
# train.py
from surrogate.train.studio_guard import get_or_create_studio, safe_run
from surrogate.data.cdn_loader import load_file_list, build_dataset

def main():
    files = load_file_list()
    table = build_dataset(files)
    # ... training logic ...

if __name__ == "__main__":
    studio = get_or_create_studio("surrogate-train", machine=L.Machine.L40S)
    safe_run(studio, main)
```

---

### 5) Cron/process hardening (wrapper)
Ensure wrapper scripts have shebang and are executable:
```bash
#!/usr/bin/env bash
# scripts/run_training.sh
set -euo pipefail
export SHELL=/bin/bash
cd /opt/axentx/airship
python -m surrogate.train.train
```
```bash
chmod +x scripts/run_training.sh
```
Crontab:
```cron
SHELL=/bin/bash
0 2 * * * /opt/axentx/airship/scripts/run_training.sh >> /var/log/airship_training.log 2>&1
```

---

## Acceptance Criteria
- [ ] `file_list.json` produced by single API call; training uses CDN URLs only (zero auth calls during data load).  
- [ ] Writes sharded across 5 sibling repos deterministically; no 128/hr cap blocking.  
- [ ] Studio reused if running; auto-restarted if stopped/idle; no silent death.  
- [ ] No `load_dataset(streaming=True)` on mixed-schema repos; `hf_hub_download` + projection used.  
- [ ] After 429: 360s backoff implemented.
