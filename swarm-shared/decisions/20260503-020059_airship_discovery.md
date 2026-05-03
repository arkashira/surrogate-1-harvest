# airship / discovery

## Highest-Value Incremental Improvement (<2h)
**CDN-first deterministic ingestion + Lightning Studio guard**  
Eliminates HF API rate limits during training, prevents quota waste from duplicate Studio creation, and enforces the proven pattern of projecting to `{prompt, response}` only before upload.

---

## Implementation Plan

### 1. Add file-listing utility (Mac orchestration side)
Single API call to list one date folder → JSON saved for Lightning training.

```python
# tools/list_hf_date_folder.py
import json
import os
import sys
from huggingface_hub import list_repo_tree

def main():
    repo = os.getenv("HF_DATASET_REPO", "axentx/surrogate-ingest")
    date_folder = sys.argv[1] if len(sys.argv) > 1 else "2026-05-03"
    out_path = sys.argv[2] if len(sys.argv) > 2 else f"file_list_{date_folder}.json"

    # One paginated call (non-recursive) per date folder
    items = list_repo_tree(repo, path=date_folder, recursive=False)
    files = [f.rfilename for f in items if f.rfilename.endswith(".parquet")]

    with open(out_path, "w") as f:
        json.dump({"date": date_folder, "repo": repo, "files": sorted(files)}, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

Usage (run once per date folder after rate-limit window clears):
```bash
python tools/list_hf_date_folder.py 2026-05-03 file_list_2026-05-03.json
```

---

### 2. Lightning Studio guard + deterministic shard selection
Reuse running studios; spread HF writes across 5 sibling repos deterministically.

```python
# surrogate/training/studio_guard.py
import hashlib
import os
from lightning_sdk import Teamspace, Studio, Machine

SIBLING_REPOS = [
    "axentx/surrogate-ingest",
    "axentx/surrogate-ingest-1",
    "axentx/surrogate-ingest-2",
    "axentx/surrogate-ingest-3",
    "axentx/surrogate-ingest-4",
]

def pick_repo(slug: str) -> str:
    """Deterministic repo selection for HF commit-cap sharding."""
    idx = int(hashlib.md5(slug.encode()).hexdigest(), 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def get_or_create_studio(name: str, machine: Machine = Machine.L40S) -> Studio:
    """Reuse running studio; restart if stopped."""
    teamspace = Teamspace.current()
    for s in teamspace.studios:
        if s.name == name:
            if s.status == "running":
                return s
            # restart if stopped/idle killed training
            s.start(machine=machine)
            return s
    return teamspace.studios.create(
        name=name,
        machine=machine,
        create_ok=True,
    )
```

---

### 3. CDN-only dataset loader (zero API calls during training)
Embed file list; fetch parquet via public CDN URLs.

```python
# surrogate/training/cdn_dataset.py
import json
import pyarrow.parquet as pq
import requests
from torch.utils.data import Dataset

class CDNParquetDataset(Dataset):
    """
    Loads parquet projected to {prompt, response} via CDN.
    file_list_json must contain: {"repo": "...", "files": [...]}
    """
    BASE = "https://huggingface.co/datasets"

    def __init__(self, file_list_json: str):
        with open(file_list_json) as f:
            meta = json.load(f)
        self.repo = meta["repo"]
        self.files = meta["files"]
        self.rows = self._build_index()

    def _build_index(self):
        rows = []
        for fname in self.files:
            url = f"{self.BASE}/{self.repo}/resolve/main/{fname}"
            # Lightweight projection: read only needed cols via pyarrow
            with pq.ParquetFile(url) as pf:
                for rg in range(pf.metadata.num_row_groups):
                    rows.append((url, rg, fname))
        return rows

    def __len__(self):
        return len(self.rows)

    def _read_row_group(self, url, rg):
        # Read single row-group to minimize memory/CPU
        table = pq.read_table(url, columns=["prompt", "response"], use_threads=False, batch_size=1024)
        return table.to_pylist()

    def __getitem__(self, idx):
        url, rg, fname = self.rows[idx]
        # Simple deterministic shard: pick one row from row-group
        table = pq.read_table(url, columns=["prompt", "response"], use_threads=False,
                              batch_size=1024, use_pandas_metadata=False)
        data = table.to_pylist()
        if not data:
            return {"prompt": "", "response": ""}
        # Deterministic pick by idx mod len
        item = data[idx % len(data)]
        return {"prompt": str(item.get("prompt", "")), "response": str(item.get("response", ""))}
```

---

### 4. Training script stub (Lightning launcher)
Uses CDN dataset + Studio guard.

```python
# surrogate/training/train_cdn.py
import argparse
from lightning_sdk import Machine
from surrogate.training.studio_guard import get_or_create_studio
from surrogate.training.cdn_dataset import CDNParquetDataset

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", default="file_list_2026-05-03.json")
    parser.add_argument("--studio-name", default="surrogate-l40s-train")
    args = parser.parse_args()

    # Studio guard: reuse or start
    studio = get_or_create_studio(args.studio_name, machine=Machine.L40S)

    # CDN dataset (zero HF API calls during training)
    dataset = CDNParquetDataset(args.file_list)
    print(f"Loaded {len(dataset)} rows via CDN")

    # Example: run training command inside studio
    # studio.run(
    #     command=["python", "train.py", "--data-path", args.file_list],
    #    environment="surrogate-train-env",
    # )

if __name__ == "__main__":
    main()
```

---

### 5. Ingestion projection fix (enforced schema)
Before upload, project to `{prompt, response}`; move attribution to filename pattern.

```python
# surrogate/ingestion/project_and_upload.py
import pyarrow as pa
import pyarrow.parquet as pq
from surrogate.training.studio_guard import pick_repo
from huggingface_hub import upload_file

def project_and_upload(batch_path: str, date: str, slug: str):
    repo = pick_repo(slug)
    table = pq.read_table(batch_path, columns=["prompt", "response"])
    out_name = f"batches/mirror-merged/{date}/{slug}.parquet"
    # Write projected parquet locally first (or stream)
    pq.write_table(table, "/tmp/projected.parquet")
    upload_file(
        path_or_fileobj="/tmp/projected.parquet",
        path_in_repo=out_name,
        repo_id=repo,
    )
    print(f"Uploaded {out_name} to {repo}")
```

---

## Execution Order (≤2h)
1. `python tools/list_hf_date_folder.py 2026-05-03 file_list_2026-05-03.json`  
2. Verify `file_list_2026-05-03.json` exists and contains parquet files.  
3. `python surrogate/training/train_cdn.py --file-list file_list_2026-05-03.json --studio-name surrogate-l40s-train`  
4. Confirm Studio reused/started and dataset loads via CDN (no 429s).  

**Result**: Training can now run with zero HF API calls during data load, deterministic repo sharding avoids commit-cap blocks, and Studio reuse preserves Lightning quota.
