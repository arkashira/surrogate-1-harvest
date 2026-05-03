# airship / frontend

## Final Synthesis (Best of Both Candidates)

**Unified Goal**: Eliminate HF API rate limits, prevent Lightning quota waste, and harden the surrogate/arkship UI against idle-stop training death — all via deterministic orchestration and CDN-first ingestion.

**Why this is highest-value now**:
- Fixes `pyarrow CastError`, `429 rate-limit`, `HF commit cap`, `Lightning idle-stop kills`, and `Lightning quota waste` in one deterministic path.
- Combines backend deterministic ingestion + CDN-only training with frontend UX guardrails.
- Uses existing patterns (CDN bypass, pre-list once, studio reuse, idle guard, one-click restart).
- No new infra — pure orchestration + small code changes + one targeted frontend addition.

---

## Implementation Plan (Backend + Frontend)

### 1. Deterministic file list (mac-side, one-time per date)
- Run `list_repo_tree(recursive=False)` per date folder for `dataset-mirror` repos.
- Save `file_list.json` with `{repo, date, files[]}`.
- Embed this JSON in the Lightning training script so training uses **CDN-only** fetches (`https://huggingface.co/datasets/.../resolve/main/...`).

### 2. Ingestion: schema projection at write time
- Before upload, project each file to `{prompt, response}` only.
- Filename pattern: `batches/mirror-merged/{date}/{slug}.parquet`.
- No `source` or `ts` columns in parquet (attribution via filename).
- Prevents `pyarrow CastError` from mixed schemas.

### 3. Lightning Studio guard (backend) — reuse + idle resilience
- Before `.run()`, list `Teamspace.studios` and reuse any running studio with matching name.
- If stopped, restart with `target.start(machine=Machine.L40S)` (or fallback to free-tier L40S).
- Wrap training calls with status check to avoid idle-stop death.

### 4. HF commit cap mitigation (if writes needed)
- Deterministic hash(slug) → pick sibling repo (0..4) to spread writes across 5 repos = 640/hr aggregate.

### 5. Training script: CDN-only dataset loader (zero API calls)
- Replace `load_dataset(streaming=True)` with CDN `wget`/`fsspec` direct fetch from `resolve/main/`.
- Use pre-generated `file_list.json` to build dataset index; zero API calls during training.

### 6. Frontend: Lightning Studio Guard UI
- Add a status indicator and one-click restart button for Lightning Studios in the surrogate/arkship UI.
- On click, call backend helper to:
  - Check studio status.
  - If stopped/idle, restart with deterministic machine selection (L40S → fallback).
  - Poll until running, then surface running state.
- Prevents UI-initiated training attempts on stopped studios and gives immediate recovery path.

---

## Code Snippets

### 1) Mac-side: generate deterministic file list (run once per date folder)

```bash
#!/usr/bin/env bash
# scripts/generate_file_list.sh
set -euo pipefail

REPO="dataset-mirror"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="file_list_${DATE}.json"

python3 - <<PY
import json, os
from huggingface_hub import HfApi

api = HfApi()
repo = "${REPO}"
date = "${DATE}"
path = f"batches/mirror-merged/{date}"

# single non-recursive call
tree = api.list_repo_tree(repo_id=repo, path=path, recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith(".parquet")]

out = {
    "repo": repo,
    "date": date,
    "path": path,
    "files": sorted(files)
}
with open("${OUT}", "w") as f:
    json.dump(out, f, indent=2)
print(f"Wrote ${OUT} with {len(files)} files")
PY
```

### 2) Ingestion: project to {prompt,response} and write deterministic filename

```python
# surrogate/ingest/project_and_upload.py
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path
import hashlib

def project_to_pair(table: pa.Table) -> pa.Table:
    # Keep only prompt/response; drop everything else
    keep = [c for c in table.column_names if c in ("prompt", "response")]
    if "prompt" not in keep or "response" not in keep:
        raise ValueError("Missing prompt or response in schema")
    return table.select(keep)

def deterministic_slug(text: str, length=12) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]

def write_projected(batch_path: Path, out_root: Path, date: str):
    table = pq.read_table(str(batch_path))
    projected = project_to_pair(table)

    slug = deterministic_slug(str(batch_path))
    out_dir = out_root / "batches" / "mirror-merged" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{slug}.parquet"

    pq.write_table(projected, str(out_file))
    return out_file
```

### 3) Lightning Studio guard (backend) + reuse

```python
# surrogate/train/lightning_guard.py
import lightning as L
import time

def get_or_create_studio(name: str, machine: L.Machine = L.Machine.L40S):
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s

    # If exists but stopped, restart
    for s in teamspace.studios:
        if s.name == name and s.status == "Stopped":
            print(f"Restarting stopped studio: {name}")
            s.start(machine=machine)
            wait_for_running(s)
            return s

    print(f"Creating new studio: {name}")
    studio = L.Studio(
        name=name,
        machine=machine,
        create_ok=True
    )
    wait_for_running(studio)
    return studio

def wait_for_running(studio, timeout=300, interval=10):
    elapsed = 0
    while elapsed < timeout:
        studio.refresh()
        if studio.status == "Running":
            return
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"Studio {studio.name} did not become running within {timeout}s")

def safe_run(studio, target, *args, **kwargs):
    studio.refresh()
    if studio.status != "Running":
        print(f"Studio stopped; restarting...")
        studio.start(machine=L.Machine.L40S)
        wait_for_running(studio)
    return target.run(*args, **kwargs)
```

### 4) Training: CDN-only dataset loader (zero API calls)

```python
# surrogate/train/cdn_dataset.py
import json
import fsspec
import pyarrow.parquet as pq
import random
import torch
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    def __init__(self, file_list_path, repo, split=None):
        with open(file_list_path) as f:
            manifest = json.load(f)
        self.repo = repo
        self.files = [f"https://huggingface.co/datasets/{repo}/resolve/main/{fn}" for fn in manifest["files"]]
        if split:
            # deterministic split
            random.Random(42).shuffle(self.files)
            n = len(self.files)
            if split == "train":
                self.files = self.files[: int(n * 0.95)]
            else:
                self.files = self.files[int(n * 0.95):]

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            files = self.files
        else:
            per_worker = len(self.files) // worker_info.num_workers
            files = self.files[worker_info.id * per_worker : (worker_info.id + 1) * per_worker]

        for url in files:
            with fsspec.open(url, "rb") as f:
                table = pq.read_table(f)
                for row in table.to_pylist():
                    yield row
```

### 5) HF commit cap: deterministic sibling repo selection (if writes
