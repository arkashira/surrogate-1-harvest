# airship / discovery

## Final Synthesized Plan (Best of Both Candidates)

**Highest-Value Incremental Improvement**  
**CDN-only ingestion + deterministic sibling-repo sharding + Studio lifecycle guard**  
- Eliminates HF API 429s during training by pre-listing once and downloading via public CDN URLs (no auth).  
- Bypasses 128/hr commit cap via deterministic sibling-repo sharding (hash-slug → repo).  
- Prevents wasted Lightning quota by reusing Running Studios and guarding against idle-stop deaths.

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Deliverable |
|------|-------|------|-------------|
| 1. Create file-list utility (Mac orchestration) | me | 15m | `scripts/list_hf_date.py` → `filelist.json` |
| 2. Add CDN-only dataset loader (project-on-read) | me | 30m | `surrogate/data/cdn_dataset.py` |
| 3. Deterministic sibling-repo sharding for writes | me | 20m | `surrogate/data/shard.py` |
| 4. Studio lifecycle guard (reuse + restart) | me | 20m | `surrogate/train/studio_guard.py` |
| 5. Wire into training entrypoint | me | 15m | `surrogate/train/train.py` |
| 6. Smoke test (dry-run list + CDN fetch) | me | 20m | logs + 1 batch verified |

---

## Code Snippets

### 1. List HF date folder once (run from Mac)
```python
# scripts/list_hf_date.py
import json, os, sys
from huggingface_hub import list_repo_tree

REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-mirror")
DATE_PATH = sys.argv[1] if len(sys.argv) > 1 else "batches/mirror-merged/2026-05-03"

# Single shallow call (no recursion into subfolders)
files = list_repo_tree(repo_id=REPO, path=DATE_PATH, recursive=False)
file_list = [f.rfilename for f in files if f.rfilename.endswith(".parquet")]

out = {"repo": REPO, "date_path": DATE_PATH, "files": file_list}
with open("filelist.json", "w") as f:
    json.dump(out, f, indent=2)
print(f"Wrote {len(file_list)} files to filelist.json")
```

Run:
```bash
# after rate-limit window clears
HF_TOKEN=... python scripts/list_hf_date.py batches/mirror-merged/2026-05-03
```

---

### 2. CDN-only dataset loader (zero API calls during training)
```python
# surrogate/data/cdn_dataset.py
import pyarrow.parquet as pq
import requests
import io
import json
import os
from typing import List, Dict

def load_filelist(path: str = "filelist.json") -> Dict:
    with open(path) as f:
        return json.load(f)

def cdn_fetch(repo: str, file_path: str) -> bytes:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

class CDNParquetDataset:
    def __init__(self, filelist_path: str = "filelist.json"):
        meta = load_filelist(filelist_path)
        self.repo = meta["repo"]
        self.files = meta["files"]

    def __iter__(self):
        for fn in self.files:
            data = cdn_fetch(self.repo, fn)
            table = pq.read_table(io.BytesIO(data))
            # Project to {prompt, response} only; attribution via filename
            df = table.select(["prompt", "response"]).to_pandas()
            df["source_file"] = fn
            yield from df.to_dict(orient="records")
```

---

### 3. Deterministic sibling-repo sharding for writes
```python
# surrogate/data/shard.py
import hashlib

SIBLINGS = [
    "axentx/surrogate-mirror",
    "axentx/surrogate-mirror-sib1",
    "axentx/surrogate-mirror-sib2",
    "axentx/surrogate-mirror-sib3",
    "axentx/surrogate-mirror-sib4",
]

def pick_repo(slug: str) -> str:
    """Deterministic shard by slug hash."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return SIBLINGS[h % len(SIBLINGS)]

def upload_parquet_shard(df_bytes: bytes, slug: str, date: str) -> str:
    from huggingface_hub import upload_file
    repo = pick_repo(slug)
    path = f"batches/mirror-merged/{date}/{slug}.parquet"
    upload_file(
        path_or_fileobj=df_bytes,
        path_in_repo=path,
        repo_id=repo,
        repo_type="dataset",
    )
    return f"{repo}/{path}"
```

---

### 4. Studio lifecycle guard (reuse + restart)
```python
# surrogate/train/studio_guard.py
from lightning import Studio, Machine, Teamspace
import time

def get_or_start_studio(name: str = "surrogate-train-l40s", machine: Machine = Machine.L40S):
    # Reuse if running
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.id}")
            return s

    # Start new (or stopped)
    studio = Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )
    if studio.status != "Running":
        print(f"Starting studio {name} on {machine}...")
        studio.start(machine=machine)
        # Wait until running
        while studio.status != "Running":
            time.sleep(10)
            studio.refresh()
    return studio

def run_with_guard(script: str, args: list[str], studio_name: str = "surrogate-train-l40s"):
    studio = get_or_start_studio(studio_name)
    run = studio.run(script, arguments=args)
    return run
```

---

### 5. Wire into training entrypoint
```python
# surrogate/train/train.py
import argparse
from surrogate.data.cdn_dataset import CDNParquetDataset
from surrogate.train.studio_guard import run_with_guard

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filelist", default="filelist.json")
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    dataset = CDNParquetDataset(args.filelist)
    # Your training loop here; dataset yields dicts with prompt/response
    for i, ex in enumerate(dataset):
        if i >= 1000:  # small dry-run
            break
        # train step...

if __name__ == "__main__":
    # For local dev (Mac orchestration)
    main()

# For Lightning Studio execution (no HF API during data load):
# run_with_guard("surrogate/train/train.py", ["--filelist", "filelist.json", "--epochs", "3"])
```

---

## Verification (smoke test)
```bash
# 1. Generate filelist (once)
python scripts/list_hf_date.py batches/mirror-merged/2026-05-03

# 2. Dry-run CDN fetch (should not hit /api/ auth)
python -c "
from surrogate.data.cdn_dataset import CDNParquetDataset
ds = CDNParquetDataset('filelist.json')
for i, ex in enumerate(ds):
    print(i, ex.get('source_file'))
    if i >= 2: break
"

# 3. Check Studio reuse path (dry import)
python -c "from surrogate.train.studio_guard import get_or_start_studio; print('guard OK')"
```

Expected: CDN downloads succeed, no 429, filelist contains parquet paths, Studio guard imports cleanly.
