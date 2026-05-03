# airship / discovery

## Highest-Value Incremental Improvement
Add an **HF CDN-bypass dataset loader with Lightning Studio reuse** to the training UI/launcher.  
- Eliminates HF API 429s during training by using CDN-only fetches.  
- Saves Lightning quota by reusing running studios instead of recreating.  
- Fits in <2h and is immediately usable for Surrogate training workflows.

---

## Implementation Plan (≤2h)

1. **Create file list artifact** (Mac orchestration, one-time or per date folder)  
   - Use `list_repo_tree(path, recursive=False)` for a single date folder (e.g., `batches/mirror-merged/2026-05-03/`).  
   - Save to `filelist.json` and commit/ship with training job.

2. **Add CDN-only dataset loader** (`surrogate/training/data/cdn_loader.py`)  
   - Accept `filelist.json` and download via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth).  
   - Parse only `{prompt, response}` at load time; ignore extra schema columns.  
   - Stream with `IterableDataset` to avoid loading all files into memory.

3. **Lightning Studio reuse + safe run wrapper** (`surrogate/training/launch.py`)  
   - List `Teamspace.studios`, reuse a running studio by name.  
   - If stopped, restart with `target.start(machine=Machine.L40S)` (respecting free-tier fallback).  
   - Before each `.run()`, check status; avoid idle-stop deaths.

4. **Wire into training UI / CLI**  
   - Add a “Train with CDN-bypass” button/command that:  
     1. Accepts date folder arg.  
     2. Generates/uses `filelist.json`.  
     3. Launches Lightning job with `cdn_loader` as dataset source.

5. **Test & verify**  
   - Run locally (Mac) to generate filelist and validate CDN downloads.  
   - Launch Lightning Studio job and confirm zero HF API calls during data loading (check logs for 429s).

---

## Code Snippets

### 1) Generate file list (Mac orchestration)
```bash
# surrogate/training/scripts/gen_filelist.sh
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-dataset"
DATE_FOLDER="${1:-batches/mirror-merged/$(date +%Y-%m-%d)}"
OUTFILE="${2:-filelist.json}"

python -c "
import json, os
from huggingface_hub import list_repo_tree

tree = list_repo_tree(repo_id='$REPO', path='$DATE_FOLDER', recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith('.parquet')]
with open('$OUTFILE', 'w') as f:
    json.dump(files, f, indent=2)
print(f'Wrote {len(files)} files to $OUTFILE')
"
```

### 2) CDN-only loader
```python
# surrogate/training/data/cdn_loader.py
import json
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset
from typing import List, Dict

HF_DATASETS_ROOT = "https://huggingface.co/datasets"

class CDNParquetIterable(IterableDataset):
    def __init__(self, repo: str, filelist_path: str, columns=("prompt", "response")):
        self.repo = repo
        self.columns = columns
        with open(filelist_path) as f:
            self.files: List[str] = json.load(f)

    def _stream_files(self):
        for rfn in self.files:
            url = f"{HF_DATASETS_ROOT}/{self.repo}/resolve/main/{rfn}"
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            # Write to temp or use pyarrow buffer
            table = pq.read_table(pq.ParquetFile(pq.BufferReader(resp.content)))
            # Project only needed columns (ignore mixed schema extras)
            if "prompt" not in table.column_names or "response" not in table.column_names:
                continue
            table = table.select(self.columns)
            for batch in table.to_batches(max_chunksize=1024):
                for i in range(batch.num_rows):
                    row = {col: batch[col][i].as_py() for col in self.columns}
                    if row.get("prompt") and row.get("response"):
                        yield row

    def __iter__(self):
        return self._stream_files()
```

### 3) Lightning Studio reuse + safe run
```python
# surrogate/training/launch.py
import os
from lightning import Lightning, Teamspace, Machine, Studio

LIGHTNING_TEAMSPACE = os.getenv("LIGHTNING_TEAMSPACE", "default")
STUDIO_NAME = os.getenv("LIGHTNING_STUDIO_NAME", "surrogate-train")
MACHINE = Machine.L40S  # free tier falls back to L40S; H200 requires paid account

def ensure_studio() -> Studio:
    ts = Teamspace(name=LIGHTNING_TEAMSPACE)
    running = [s for s in ts.studios if s.name == STUDIO_NAME and s.status == "running"]
    if running:
        print(f"Reusing running studio: {STUDIO_NAME}")
        return running[0]

    stopped = [s for s in ts.studios if s.name == STUDIO_NAME]
    if stopped:
        s = stopped[0]
        print(f"Restarting stopped studio: {STUDIO_NAME}")
        s.start(machine=MACHINE)
        return s

    print(f"Creating new studio: {STUDIO_NAME}")
    return Studio.create(name=STUDIO_NAME, machine=MACHINE, create_ok=True)

def run_training(script: str, args: List[str]):
    studio = ensure_studio()
    # Check status before run to avoid idle-stop deaths
    if studio.status != "running":
        print(f"Studio not running (status={studio.status}), restarting...")
        studio.start(machine=MACHINE)

    # Example run: executes training script inside studio
    run = studio.run(
        command=["python", script] + args,
        environment={"HF_DATASETS_OFFLINE": "1", "HF_HUB_OFFLINE": "0"},
    )
    print(f"Started run: {run.id}")
    return run

if __name__ == "__main__":
    import sys
    run_training(
        script="train.py",
        args=["--data-filelist", "filelist.json", "--epochs", "1"],
    )
```

### 4) Minimal train.py usage
```python
# surrogate/training/train.py
import argparse
from torch.utils.data import DataLoader
from surrogate.training.data.cdn_loader import CDNParquetIterable

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-filelist", required=True)
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    dataset = CDNParquetIterable(
        repo="axentx/surrogate-dataset",
        filelist_path=args.data_filelist,
        columns=("prompt", "response"),
    )
    loader = DataLoader(dataset, batch_size=8)

    for epoch in range(args.epochs):
        for batch in loader:
            # Replace with actual surrogate training step
            print(f"Epoch {epoch} batch: prompts={len(batch['prompt'])}")
            # train_step(batch)

if __name__ == "__main__":
    main()
```

---

## Acceptance Criteria
- [ ] `gen_filelist.sh` produces valid `filelist.json` for a date folder.  
- [ ] `cdn_loader.py` streams parquet files via CDN without HF API auth/429s.  
- [ ] `launch.py` reuses a running Lightning Studio or restarts safely.  
- [ ] Training run completes without HF API rate-limit errors.
