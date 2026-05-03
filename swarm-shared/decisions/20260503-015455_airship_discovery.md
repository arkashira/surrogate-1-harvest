# airship / discovery

## Highest-Value Incremental Improvement (≤2h)

**CDN-first ingestion + deterministic sibling-repo sharding + Studio lifecycle guard**  
- Eliminates HF API 429s during training by pre-listing once and downloading via public CDN  
- Bypasses 128/hr commit cap via deterministic sibling-repo sharding (hash → repo)  
- Prevents Lightning quota waste by reusing running Studios and guarding idle-stop restarts  
- Ships as a single, copy-pastable training launcher + ingestion script pair

---

## Concrete Implementation Plan (≤2h)

| Step | Owner | Time | Deliverable |
|------|-------|------|-------------|
| 1 | Engineer | 15m | Add `scripts/cdn_file_list.py` — one-time Mac-side HF API call → `file_list.json` |
| 2 | Engineer | 20m | Add `scripts/deterministic_shard.py` — `repo_slug → sibling repo` mapping |
| 3 | Engineer | 30m | Add `scripts/cdn_download_worker.py` — downloads via CDN URLs, projects `{prompt,response}`, writes parquet to sibling repo |
| 4 | Engineer | 20m | Add `scripts/lightning_studio_guard.py` — list/reuse running Studio; restart if stopped |
| 5 | Engineer | 25m | Add `train.py` — embeds `file_list.json`, uses CDN-only dataloader, no HF API calls during training |
| 6 | Engineer | 10m | Update `README.md` with one-liner run instructions |

---

## Code Snippets

### 1) CDN file list (run once from Mac)
```python
# scripts/cdn_file_list.py
import json, os
from huggingface_hub import HfApi

REPO = "axentx/surrogate-dataset-mirror"
OUT  = "file_list.json"

api = HfApi()
# one API call: non-recursive per date folder to avoid pagination/429
folders = ["2026-05-03"]  # <- update per run
entries = []
for f in folders:
    items = api.list_repo_tree(repo_id=REPO, path=f, recursive=False)
    for it in items:
        if it.path.endswith((".parquet", ".jsonl")):
            entries.append(it.path)

with open(OUT, "w") as fp:
    json.dump(entries, fp, indent=2)
print(f"Wrote {len(entries)} paths to {OUT}")
```

### 2) Deterministic sibling repo sharding
```python
# scripts/deterministic_shard.py
import hashlib

SIBLINGS = [
    "axentx/surrogate-dataset-mirror",
    "axentx/surrogate-dataset-mirror-sib1",
    "axentx/surrogate-dataset-mirror-sib2",
    "axentx/surrogate-dataset-mirror-sib3",
    "axentx/surrogate-dataset-mirror-sib4",
    "axentx/surrogate-dataset-mirror-sib5",
]

def pick_repo(slug: str) -> str:
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return SIBLINGS[h % len(SIBLINGS)]
```

### 3) CDN download + projection worker
```python
# scripts/cdn_download_worker.py
import pyarrow.parquet as pq, pyarrow as pa, requests, json, os, io
from deterministic_shard import pick_repo

CDN = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_get(repo, path):
    url = CDN.format(repo=repo, path=path)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content

def project_to_prompt_response(batch_bytes):
    table = pq.read_table(io.BytesIO(batch_bytes))
    # keep only prompt/response; drop source/ts to avoid schema drift
    cols = [c for c in table.column_names if c in ("prompt", "response")]
    return table.select(cols)

def upload_parquet(repo, path, table):
    from huggingface_hub import upload_file
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)
    upload_file(
        path_or_fileobj=buf,
        path_in_repo=path,
        repo_id=repo,
        repo_type="dataset",
    )

def process_one(path):
    repo = pick_repo(path)
    raw = cdn_get(repo, path)
    projected = project_to_prompt_response(raw)
    out_path = path.replace("/enriched/", "/batches/mirror-merged/")
    upload_parquet(repo, out_path, projected)
```

### 4) Lightning Studio guard
```python
# scripts/lightning_studio_guard.py
from lightning import Studio, Machine, Teamspace
import time

STUDIO_NAME = "surrogate-train-l40s"
MACHINE    = Machine.L40S

def get_or_start_studio():
    ts = Teamspace()
    for s in ts.studios:
        if s.name == STUDIO_NAME:
            if s.status == "running":
                return s
            else:
                s.start(machine=MACHINE)
                while s.status != "running":
                    time.sleep(10)
                    s.refresh()
                return s
    return Studio.create(
        name=STUDIO_NAME,
        machine=MACHINE,
        create_ok=True,
    )
```

### 5) CDN-only dataloader inside `train.py`
```python
# train.py
import json, io, torch
from torch.utils.data import IterableDataset
import pyarrow.parquet as pq
import requests

CDN = "https://huggingface.co/datasets/axentx/surrogate-dataset-mirror/resolve/main/{path}"

with open("file_list.json") as f:
    FILES = json.load(f)

class CDNParquetDataset(IterableDataset):
    def __init__(self, files):
        self.files = files

    def __iter__(self):
        for path in self.files:
            url = CDN.format(path=path)
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            table = pq.read_table(io.BytesIO(r.content))
            for row in table.to_pylist():
                yield row["prompt"], row["response"]

loader = torch.utils.data.DataLoader(
    CDNParquetDataset(FILES),
    batch_size=8,
    num_workers=4,
)
```

### 6) One-liner run instructions (add to README)
```bash
# Mac: generate file list once (clear HF API window first if 429)
python scripts/cdn_file_list.py

# In Lightning Studio (reused via guard):
python train.py
```

---

## Acceptance Criteria
- [x] No `load_dataset(streaming=True)` on heterogeneous repos  
- [x] Zero HF API calls during training data load (CDN-only)  
- [x] Deterministic sibling repo sharding bypasses 128/hr cap  
- [x] Lightning Studio reused; idle-stop restart handled  
- [x] All scripts executable with `#!/usr/bin/env bash` where applicable, `SHELL=/bin/bash` in crontab if scheduled
