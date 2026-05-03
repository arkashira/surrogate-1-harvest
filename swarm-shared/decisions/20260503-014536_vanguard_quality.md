# vanguard / quality

## Final Synthesized Implementation Plan  
*(Best parts merged, contradictions resolved in favor of correctness + concrete actionability)*

---

### 1. Diagnosis (consensus)

- **Quota/429 root cause**: Frontend and training both call authenticated `list_repo_tree` on every data-source selection or training launch.  
- **No persisted file list**: Each run re-enumerates instead of using a cached manifest → repeated API calls and schema surprises.  
- **Schema mismatch**: Ingestion writes extra columns (`source`, `ts`) into `enriched/`; downstream training expects strict `{prompt, response}` and fails with `pyarrow.CastError`.  
- **CDN bypass missing**: Training uses `load_dataset`/`hf_hub_download` (API path) instead of `resolve/main/` CDN URLs.  
- **Studio lifecycle fragile**: Recreating studios wastes quota; idle-stop kills training.

---

### 2. High-leverage change (scope)

Create a small, high-leverage quality layer in `/opt/axentx/vanguard/` that:

1. **Projects raw files to strict `{prompt, response}` pairs** and writes clean Parquet to `batches/mirror-merged/{date}/{slug}.parquet`.  
2. **Produces a persisted `file_list.json`** for `(repo, dateFolder)` (run once per folder after rate-limit window).  
3. **Trains via CDN-only URLs** (`resolve/main/...`) with zero authenticated API calls during data loading.  
4. **Reuses Lightning Studio by name**; restarts only if stopped.

---

### 3. Implementation (single, executable plan)

#### 3.0 Environment guard
```bash
# Ensure orchestration runs with proper Bash environment
export SHELL=/bin/bash
set -euo pipefail
```

#### 3.1 Ingestion: project-to-pair.py  (new)
Location: `/opt/axentx/vanguard/project-to-pair.py`

```python
#!/usr/bin/env python3
"""
Project raw HF files to {prompt,response} pairs and write clean Parquet.
Usage: ./project-to-pair.py <repo> <dateFolder> <out_dir>
"""
import os
import sys
import json
import uuid
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import list_repo_tree, hf_hub_download

def main():
    if len(sys.argv) < 4:
        print("Usage: project-to-pair.py <repo> <dateFolder> <out_dir>")
        sys.exit(1)

    repo = sys.argv[1]
    date_folder = sys.argv[2]
    out_dir = sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)

    # Non-recursive, single-level list to minimize quota burn
    items = list_repo_tree(repo, path=date_folder, recursive=False)
    rows = []

    for f in items.get("files", []):
        path = f["path"]
        if not path.lower().endswith((".jsonl", ".json", ".parquet", ".csv")):
            continue

        local_path = hf_hub_download(repo_id=repo, filename=path, repo_type="dataset")

        if path.endswith(".parquet"):
            tbl = pq.read_table(local_path, columns=["prompt", "response"])
            for b in tbl.to_batches():
                for i in range(b.num_rows):
                    rows.append({
                        "prompt": b["prompt"][i].as_py(),
                        "response": b["response"][i].as_py(),
                    })
        else:
            with open(local_path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    rows.append({
                        "prompt": obj.get("prompt") or obj.get("input") or "",
                        "response": obj.get("response") or obj.get("output") or "",
                    })

    schema = pa.schema([
        pa.field("prompt", pa.string()),
        pa.field("response", pa.string()),
    ])
    tbl = pa.Table.from_pylist(rows, schema=schema)
    out_path = os.path.join(out_dir, f"{date_folder}-{str(uuid.uuid4())[:8]}.parquet")
    pq.write_table(tbl, out_path)
    print(json.dumps({"out": out_path, "rows": len(rows)}))

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/project-to-pair.py
```

---

#### 3.2 File manifest: list-once.sh  (new)
Location: `/opt/axentx/vanguard/list-once.sh`

```bash
#!/usr/bin/env bash
# Run once from Mac after rate-limit window clears.
# Produces file_list.json for CDN-only training.
set -euo pipefail

REPO="${1:-datasets/your-repo}"
DATEFOLDER="${2:-2026-05-03}"
OUT="${3:-file_list.json}"

python3 - "$REPO" "$DATEFOLDER" "$OUT" <<'PY'
import json
import sys
from huggingface_hub import list_repo_tree

repo, date_folder, out = sys.argv[1], sys.argv[2], sys.argv[3]
items = list_repo_tree(repo, path=date_folder, recursive=False)

files = [
    f["path"]
    for f in items.get("files", [])
    if f["path"].lower().endswith((".parquet", ".jsonl", ".json", ".csv"))
]

with open(out, "w") as f:
    json.dump({"repo": repo, "date_folder": date_folder, "files": files}, f, indent=2)

print(f"Wrote {len(files)} files to {out}")
PY
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/list-once.sh
```

---

#### 3.3 Training: train.py  (create/update)
Location: `/opt/axentx/vanguard/train.py`

```python
#!/usr/bin/env python3
"""
CDN-only surrogate-1 training data loader.
Run on Lightning Studio (reuse existing).
"""
import json
import os
import io
from typing import Iterator

import requests
import pyarrow.parquet as pq
import torch
from torch.utils.data import IterableDataset, DataLoader
import lightning as L

HF_DATASET_REPO = "datasets/your-repo"
FILE_LIST = "file_list.json"  # produced by list-once.sh

class CDNParquetIterable(IterableDataset):
    def __init__(self, file_list_path: str):
        with open(file_list_path) as f:
            cfg = json.load(f)
        self.repo = cfg["repo"]
        self.folder = cfg["date_folder"]
        self.files = cfg["files"]

    def _stream_file(self, path: str) -> Iterator[dict]:
        # CDN bypass: no Authorization header
        url = f"https://huggingface.co/datasets/{self.repo}/resolve/main/{path}"
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        tbl = pq.read_table(buf, columns=["prompt", "response"])
        for b in tbl.to_batches():
            for i in range(b.num_rows):
                yield {
                    "prompt": b["prompt"][i].as_py(),
                    "response": b["response"][i].as_py(),
                }

    def __iter__(self) -> Iterator[dict]:
        for p in self.files:
            yield from self._stream_file(p)

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self):
        super().__init__()
        self.ds = CDNParquetIterable(FILE_LIST)

    def train_dataloader(self) -> DataLoader:
        return DataLoader(self.ds, batch_size=8, num_workers=0)

class SurrogateModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        # Replace with real model/tokenizer
        self.net = torch.nn.Linear(1024, 1024)

    def training_step(self, batch, batch_idx):
        # Minimal step — swap for real forward pass
        x = torch.randn(8, 1024, device=self.device)
        loss = self.net(x).sum() * 0.0
        self
