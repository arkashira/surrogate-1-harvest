# airship / discovery

## Implementation Plan: Airship Discovery — CDN-Only Deterministic Ingestion

**Scope**: Harden `airship discover` into a deterministic, CDN-only orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing reproducible file lists and safe ingestion artifacts.  
**Timebox**: ≤2h  
**Key constraints**:
- No `load_dataset(streaming=True)` on heterogeneous repos.
- No recursive `list_repo_files`; use `list_repo_tree(path, recursive=False)` once per folder (or skip entirely if we embed file lists).
- CDN-only data fetch during training (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — zero API calls at runtime.
- Mac runs orchestration only; training → Lightning Studio; ingestion → HF Space.

---

### 1) High-value deliverable (ships in <2h)
Add a **deterministic discovery pipeline** that:
1. Pre-lists files for a single date folder (once) and saves `file-list.json`.
2. Embeds that list into the training script so Lightning workers fetch via CDN only.
3. Downloads individual files via `hf_hub_download` (or raw CDN) and projects to `{prompt, response}` at parse time (no schema assumptions).
4. Outputs sharded `batches/mirror-merged/{date}/{slug}.parquet` with no extra metadata columns.

---

### 2) Concrete steps + code snippets

#### A) Discovery orchestrator (mac/CLI only)
```bash
# /opt/axentx/airship/scripts/discover.sh
#!/usr/bin/env bash
set -euo pipefail
export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
export HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-$HF_HOME/datasets}"

REPO="axentx/surrogate-ingest"   # example
DATE_DIR="2026-05-02"             # parameterized
OUT_DIR="file-lists"
mkdir -p "$OUT_DIR"

# 1) List once (non-recursive) — respects rate limits
python3 -c "
from huggingface_hub import list_repo_tree
import json, os
items = list_repo_tree('$REPO', path='$DATE_DIR', recursive=False)
files = [f.rfilename for f in items if f.type == 'file']
os.makedirs('$OUT_DIR', exist_ok=True)
with open('$OUT_DIR/${DATE_DIR}.json', 'w') as fp:
    json.dump(files, fp, indent=2)
print(f'Found {len(files)} files')
"

# 2) Optional: quick schema probe on first N files to confirm {prompt,response}
# (skip if heterogeneous; rely on projection at parse time)
```

#### B) Safe ingestion worker (runs in HF Space or CI)
```python
# /opt/axentx/airship/ingest/project_and_upload.py
#!/usr/bin/env python3
import os, json, pyarrow as pa, pyarrow.parquet as pq, hashlib
from huggingface_hub import hf_hub_download, upload_folder
from pathlib import Path

REPO = os.getenv("INGEST_REPO", "axentx/surrogate-ingest")
DATE_DIR = os.getenv("DATE_DIR", "2026-05-02")
FILE_LIST = json.loads(Path(f"file-lists/{DATE_DIR}.json").read_text())

def project_to_pr(file_path: str) -> dict:
    # Lightweight projection: only extract prompt/response fields
    # Adapt parser per known source if needed; fail closed on missing keys
    import json as _json
    try:
        raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

out_dir = Path(f"batches/mirror-merged/{DATE_DIR}")
out_dir.mkdir(parents=True, exist_ok=True)

rows = []
for rel in FILE_LIST:
    local_path = hf_hub_download(repo_id=REPO, filename=f"{DATE_DIR}/{rel}")
    try:
        rec = project_to_pr(local_path)
        rec["_slug"] = hashlib.sha256(rel.encode()).hexdigest()[:12]
        rows.append(rec)
    except Exception as exc:
        print(f"Skip {rel}: {exc}")
        continue

if rows:
    table = pa.Table.from_pylist(rows, schema=pa.schema([
        ("prompt", pa.string()),
        ("response", pa.string()),
        ("_slug", pa.string()),
    ]))
    parquet_path = out_dir / f"{DATE_DIR}.parquet"
    pq.write_table(table, parquet_path)

    # Upload shards only (no extra columns)
    upload_folder(
        repo_id=REPO,
        folder_path=str(out_dir),
        path_in_repo=f"batches/mirror-merged/{DATE_DIR}",
        commit_message=f"Add {DATE_DIR} shards"
    )
```

#### C) Lightning training script (CDN-only)
```python
# /opt/axentx/airship/train.py
import json, os, pyarrow.parquet as pq, requests
from torch.utils.data import IterableDataset, DataLoader

REPO = "axentx/surrogate-ingest"
DATE_DIR = "2026-05-02"
FILE_LIST_PATH = "file-lists/2026-05-02.json"

with open(FILE_LIST_PATH) as f:
    FILES = json.load(f)

class CDNParquetDataset(IterableDataset):
    def __init__(self, files, base_url="https://huggingface.co/datasets"):
        self.files = files
        self.base_url = base_url

    def _stream_shards(self):
        for fn in self.files:
            # Expect shards at batches/mirror-merged/{DATE_DIR}/{DATE_DIR}.parquet
            url = f"{self.base_url}/{REPO}/resolve/main/batches/mirror-merged/{DATE_DIR}/{DATE_DIR}.parquet"
            # Lightweight: download once per worker and iterate rows
            # For large shards, consider partial reads via pyarrow.parquet.ParquetFile
            yield pq.read_table(url).to_pylist()

    def __iter__(self):
        for batch in self._stream_shards():
            for row in batch:
                yield row["prompt"], row["response"]

# Use in Lightning DataModule
def train_dataloader():
    ds = CDNParquetDataset(FILES)
    return DataLoader(ds, batch_size=8, num_workers=4)
```

#### D) Lightning Studio reuse guard (save quota)
```python
# /opt/axentx/airship/lightning_launcher.py
from lightning import Studio, Teamspace, Machine

def get_or_create_studio(name="surrogate-train", machine=Machine.L40S):
    studios = Teamspace.studios()
    running = [s for s in studios if s.name == name and s.status == "Running"]
    if running:
        print(f"Reusing running studio: {running[0].id}")
        return running[0]
    print("Creating new studio (or reusing stopped one)...")
    return Studio(name=name, create_ok=True).start(machine=machine)

# Always check status before .run()
studio = get_or_create_studio()
if studio.status != "Running":
    studio.start(machine=Machine.L40S)
studio.run(["python", "train.py"])
```

---

### 3) Operational notes (apply immediately)
- **Crontab**: set `SHELL=/bin/bash` and invoke wrappers via `bash <script> "$@"` to avoid exec errors (active-learning/opus-pr-reviewer pattern).
- **HF API**: after 429, wait 360s before retry; avoid recursive `list_repo_files`.
- **Schema safety**: never rely on `load_dataset(streaming=True)` for mixed-schema repos; always project at parse time.
- **Attribution**: keep attribution in filename patterns (`batches/mirror-merged/{date}/{slug}.parquet`), not extra columns.
- **Kaggle**: use raw HTTP Bearer auth for KGAT; set `isPrivate=True` to skip phone verification.

---

### 4) Acceptance criteria (≤2h)
- `discover.sh` produces deterministic `file-lists/{DATE}.json`.
- Ingestion script downloads via CDN/`hf_hub_download`, projects to `{
