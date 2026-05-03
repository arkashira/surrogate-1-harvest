# airship / discovery

## Highest-Value Incremental Improvement (<2h)

**Goal**: Eliminate HF API rate limits and Lightning quota waste during Surrogate training by implementing **CDN-first deterministic ingestion** + **Lightning Studio guard with reuse**.

**Why this ships maximum value in <2h**:
- Removes the 429/1000 req/5min HF API bottleneck during training
- Prevents 80hr/mo Lightning quota waste from idle-stop/recreate cycles
- Uses existing patterns (CDN bypass, studio reuse) — no new infra
- Pure code changes, no infra spin-up

---

## Implementation Plan

### 1. Mac-side: Pre-list HF files → JSON manifest (5 min)
Run once per date folder after rate-limit window clears. Embed path list in training script so Lightning workers do **zero API calls** during data loading.

```bash
# Run on Mac (or wherever HF API token lives)
python scripts/generate_hf_filelist.py \
  --repo "datasets/airship-mirror" \
  --date "2026-04-29" \
  --out "data/filelist_2026-04-29.json"
```

### 2. `generate_hf_filelist.py` (10 min)
```python
#!/usr/bin/env python3
"""
Generate deterministic CDN file list for HF dataset folder.
Bypasses HF API during training — workers use raw CDN URLs only.
"""
import json
import argparse
from pathlib import Path
from huggingface_hub import list_repo_tree

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)          # e.g. 2026-04-29
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    # Single non-recursive call per folder (avoids 100x pagination)
    folder_path = f"enriched/{args.date}"
    entries = list_repo_tree(
        repo_id=args.repo,
        path=folder_path,
        recursive=False
    )

    # Keep only parquet files; build CDN URLs (no auth, no API during training)
    files = []
    for entry in entries:
        if entry.path.endswith(".parquet"):
            cdn_url = (
                f"https://huggingface.co/datasets/{args.repo}"
                f"/resolve/main/{entry.path}"
            )
            files.append({
                "path": entry.path,
                "cdn_url": cdn_url,
                "size": getattr(entry, "size", None)
            })

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "folder": folder_path,
        "files": sorted(files, key=lambda x: x["path"]),
        "total_files": len(files)
    }

    Path(args.out).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {args.out}")

if __name__ == "__main__":
    main()
```

### 3. `surrogate/train.py` patch (20 min)
Replace `load_dataset(streaming=True)` with CDN-only `pyarrow` reads using the pre-generated manifest.

```python
# surrogate/train.py  (add near top)
import json
import pyarrow.parquet as pq
from torch.utils.data import IterableDataset
import requests
import io
import os

class CDNParquetDataset(IterableDataset):
    """
    Zero HF API calls during training.
    Uses pre-generated filelist JSON with raw CDN URLs.
    """
    def __init__(self, filelist_path, project_cols=("prompt", "response")):
        with open(filelist_path) as f:
            manifest = json.load(f)
        self.urls = [f["cdn_url"] for f in manifest["files"]]
        self.project_cols = project_cols

    def __iter__(self):
        for url in self.urls:
            # CDN download (no auth header) — bypasses HF API rate limits
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            # Project only required columns (mixed-schema safe)
            for col in self.project_cols:
                if col not in table.column_names:
                    table = table.append_column(col, [None] * len(table))
            table = table.select(self.project_cols)
            for batch in table.to_batches(max_chunksize=512):
                yield {k: batch[k].to_pylist() for k in self.project_cols}
```

Update data loader usage:
```python
# In your training launcher
train_dataset = CDNParquetDataset(
    filelist_path="data/filelist_2026-04-29.json",
    project_cols=("prompt", "response")
)
```

### 4. Lightning Studio guard + reuse (15 min)
Prevent quota waste by reusing running studios and guarding against idle-stop restarts.

```python
# surrogate/lightning_utils.py
from lightning import Lightning, Teamspace, Machine
import time

def get_or_create_studio(name: str, machine: Machine = Machine.L40S):
    """
    Reuse running studio; never recreate if already running.
    Prevents 80hr/mo quota waste.
    """
    ls = Lightning()
    teamspace = Teamspace()

    # Reuse if running
    for studio in teamspace.studios:
        if studio.name == name and studio.status == "running":
            print(f"Reusing running studio: {name}")
            return studio

    # Create if not exists (or was stopped)
    print(f"Creating studio: {name}")
    return teamspace.create_studio(
        name=name,
        machine=machine,
        create_ok=True
    )

def run_with_studio_guard(studio, target_script: str, args: list):
    """
    Guard against idle-stop death: check status before run.
    Restart machine if stopped.
    """
    if studio.status != "running":
        print(f"Studio stopped, restarting on {studio.machine}")
        studio.start(machine=studio.machine)
        # Wait for ready
        for _ in range(30):
            studio.refresh()
            if studio.status == "running":
                break
            time.sleep(10)

    return studio.run(
        target=target_script,
        args=args
    )
```

### 5. Launcher script (5 min)
Wire it together in `scripts/run_training.sh`:

```bash
#!/usr/bin/env bash
# scripts/run_training.sh
set -euo pipefail

# Ensure bash (per cron/wrapper lessons)
export SHELL=/bin/bash

cd /opt/axentx/airship/surrogate

# 1) Generate filelist once (Mac side or CI) — skip if already exists
if [ ! -f "data/filelist_$(date +%Y-%m-%d).json" ]; then
    python scripts/generate_hf_filelist.py \
        --repo "datasets/airship-mirror" \
        --date "$(date +%Y-%m-%d)" \
        --out "data/filelist_$(date +%Y-%m-%d).json"
fi

# 2) Lightning studio reuse + guard
python -c "
from lightning_utils import get_or_create_studio, run_with_studio_guard
from lightning import Machine

studio = get_or_create_studio('surrogate-train-l40s', Machine.L40S)
run_with_studio_guard(
    studio,
    target_script='train.py',
    args=['--filelist', 'data/filelist_$(date +%Y-%m-%d).json', '--epochs', '3']
)
"
```

Make executable:
```bash
chmod +x scripts/run_training.sh
```

---

## Verification Checklist

- [ ] `generate_hf_filelist.py` produces valid JSON with CDN URLs
- [ ] `CDNParquetDataset` loads parquet without `load_dataset` (zero HF API calls)
- [ ] `get_or_create_studio` reuses running studio (check Lightning UI)
- [ ] `run_with_studio_guard` restarts on stopped state
- [ ] Training runs end-to-end with no 429 errors

## Tags
#surrogate-1 #cdn-bypass #lightning-ai #quota-optimization #deterministic-ingestion
