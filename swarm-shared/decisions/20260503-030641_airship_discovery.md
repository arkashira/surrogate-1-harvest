# airship / discovery

## Highest-Value Incremental Improvement (<2h)
**Ship a resilient Surrogate training launcher** that:
1. Uses HF CDN bypass (no API auth during training) to eliminate 429s while loading data.
2. Pre-lists one date folder once, embeds file list in training script, so Lightning does zero API calls during data load.
3. Reuses a running Lightning Studio and auto-restarts on idle-stop so iteration is <2 min and never blocked by quota/timeouts.

---

## Implementation Plan

### 1. Add launcher script (`scripts/run_surrogate_train.py`)
- Runs on Mac (orchestration only).
- Uses HF API only once (after rate-limit window) to list a single date folder.
- Saves `file_list.json` into the Lightning run.
- Starts/reuses a Lightning Studio with L40S (falls back to free-tier if needed).
- Calls `train.py` with `--file_list file_list.json`.
- Before `.run()`, checks studio status; if stopped, restarts machine.

### 2. Update training script (`surrogate/train.py`)
- Accepts `--file_list` pointing to JSON with CDN paths.
- Uses `hf_hub_download` per file (or direct CDN fetch) with no `load_dataset(streaming=True)` on mixed-schema repo.
- Projects to `{prompt, response}` only at parse time.
- Attribution via filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`), no extra metadata columns.

### 3. Lightning Studio reuse + idle-stop resilience
- List `Teamspace.studios`, reuse running studio by name.
- If stopped, `studio.start(machine=Machine.L40S)` (or fallback).
- Guard every `.run()` with status check to avoid idle-kill.

---

## Code Snippets

### `scripts/run_surrogate_train.py`
```python
#!/usr/bin/env python3
"""
Orchestrator (run on Mac).
- Lists one date folder via HF API once.
- Embeds CDN file list for zero-API training.
- Reuses or restarts Lightning Studio to survive idle-stop.
"""
import json
import os
import time
from pathlib import Path

from lightning_sdk import Machine, Teamspace, Studio
from huggingface_hub import HfApi, list_repo_tree

HF_REPO = "axentx/surrogate-mirror"        # example
DATE_FOLDER = "2026-05-03"                 # parameterized in practice
STUDIO_NAME = "surrogate-train-l40s"
TEAMSPACE = "axentx"
LOCAL_FILE_LIST = Path("file_list.json")

api = HfApi()

def list_date_files(repo: str, date_folder: str) -> list[str]:
    """Single API call: list files in one date folder (non-recursive)."""
    entries = list_repo_tree(path=date_folder, repo_id=repo, recursive=False)
    # Keep only files we want to train on (e.g., parquet)
    files = [e.path for e in entries if e.type == "file" and e.path.endswith(".parquet")]
    return files

def build_cdn_urls(files: list[str], repo: str) -> list[str]:
    """CDN URLs bypass auth/rate-limits during training."""
    base = f"https://huggingface.co/datasets/{repo}/resolve/main"
    return [f"{base}/{f}" for f in files]

def get_or_create_studio():
    teamspace = Teamspace(TEAMSPACE)
    running = [s for s in teamspace.studios if s.name == STUDIO_NAME and s.status == "running"]
    if running:
        print(f"Reusing running studio: {STUDIO_NAME}")
        return running[0]

    stopped = [s for s in teamspace.studios if s.name == STUDIO_NAME and s.status == "stopped"]
    if stopped:
        studio = stopped[0]
        print(f"Starting stopped studio: {STUDIO_NAME}")
        # Prefer L40S; fallback to free-tier compatible machine
        try:
            studio.start(machine=Machine.L40S)
        except Exception:
            print("L40S unavailable, falling back to default (free-tier) machine")
            studio.start()
        return studio

    print(f"Creating new studio: {STUDIO_NAME}")
    try:
        return Studio.create(
            name=STUDIO_NAME,
            teamspace=TEAMSPACE,
            machine=Machine.L40S,
            python_version="3.10",
        )
    except Exception:
        print("L40S unavailable, falling back to default (free-tier) machine")
        return Studio.create(
            name=STUDIO_NAME,
            teamspace=TEAMSPACE,
            python_version="3.10",
        )

def wait_for_ready(studio, timeout=300):
    elapsed = 0
    while elapsed < timeout:
        studio.refresh()
        if studio.status == "running":
            print("Studio is running")
            return
        print(f"Studio status: {studio.status}, waiting...")
        time.sleep(15)
        elapsed += 15
    raise TimeoutError("Studio did not become ready in time")

def main():
    # 1) List files once (do this after HF rate-limit window clears if needed)
    files = list_date_files(HF_REPO, DATE_FOLDER)
    if not files:
        raise RuntimeError(f"No parquet files found in {HF_REPO}/{DATE_FOLDER}")
    urls = build_cdn_urls(files, HF_REPO)
    LOCAL_FILE_LIST.write_text(json.dumps({"repo": HF_REPO, "files": files, "urls": urls}, indent=2))
    print(f"Saved {len(urls)} file URLs to {LOCAL_FILE_LIST}")

    # 2) Studio reuse + idle-stop resilience
    studio = get_or_create_studio()
    wait_for_ready(studio)

    # 3) Run training (zero HF API calls during data load)
    train_script = Path("surrogate/train.py").absolute()
    # Pass file list so training uses CDN-only fetches
    run = studio.run(
        command=[
            "python", str(train_script),
            "--file_list", str(LOCAL_FILE_LIST.absolute()),
            "--date_folder", DATE_FOLDER,
        ],
        wait=False,  # non-blocking; monitor separately if desired
    )
    print(f"Started run {run.name} in studio {STUDIO_NAME}")

if __name__ == "__main__":
    main()
```

### `surrogate/train.py` (minimal diff)
```python
import argparse
import json
from pathlib import Path

import pyarrow.parquet as pq
import torch
from torch.utils.data import Dataset, DataLoader

# Optional: direct CDN fetch without auth
import requests
from tqdm import tqdm

class CDNParquetDataset(Dataset):
    """
    Loads parquet files from CDN URLs (no HF API during training).
    Projects to {prompt, response} only.
    Attribution via filename pattern (batches/mirror-merged/{date}/{slug}.parquet).
    """
    def __init__(self, file_list_path: str, cache_dir: str = ".cache_cdn"):
        super().__init__()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

        with open(file_list_path) as f:
            payload = json.load(f)
        self.urls = payload.get("urls") or []
        if not self.urls:
            raise ValueError("No URLs in file_list")

        # Build local cache of parquet row counts for indexing
        self.file_infos = []
        total = 0
        for url in tqdm(self.urls, desc="Indexing CDN parquets"):
            local = self._cached_path(url)
            table = pq.read_table(local, columns=["prompt", "response"])
            num_rows = table.num_rows
            self.file_infos.append((local, num_rows, total))
            total += num_rows
        self.length = total

    def _cached_path(self, url: str) -> Path:
        name = url.split("/")[-1]
        local = self.cache_dir / name
        if local.exists():
            return local
        # CDN fetch (no auth)
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        local.write_bytes(resp.content)
        return local

    def __len__(self):
        return self.length

    def _locate(self, idx):
        for local, num_rows, offset in self.file_infos:
            if idx < offset + num_rows:
                return
