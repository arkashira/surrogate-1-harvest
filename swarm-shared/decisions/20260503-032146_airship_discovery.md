# airship / discovery

## Final Implementation Plan — airship/surrogate (≤2h)

**Highest-value incremental improvement:**  
Make Surrogate training HF-rate-limit-proof and Lightning-idle-resilient by embedding a CDN-only file list and adding auto-recovery for idle timeouts.

### Why this ships value in <2h
- Eliminates the most common training failure modes (HF 429, Lightning idle timeouts) with minimal code changes.
- Uses CDN-only fetches during training to bypass HF API rate limits entirely.
- Adds automatic recovery so long-running training jobs survive idle stops.
- Small, targeted changes (one CLI helper + one training script patch + one launcher wrapper) that reuse existing patterns.

---

### Concrete steps (≤2h)

1. **Add Mac-side helper to pre-list files and emit JSON**  
   - Path: `scripts/list_hf_date_folder.py`  
   - Single `list_repo_tree(path, recursive=False)` call for one date folder.  
   - Save to `training/filelists/{date}.json`.  
   - Commit to repo so training script can load it.

2. **Patch surrogate training script to use CDN-only fetches**  
   - Path: `surrogate/train.py` (or wherever dataset loading happens).  
   - Replace `load_dataset(streaming=True)` with local filelist + direct CDN fetches (or `hf_hub_download` with CDN).  
   - Project to `{prompt, response}` only at parse time.  
   - No `source`/`ts` columns; move attribution to filename pattern `batches/mirror-merged/{date}/{slug}.parquet`.

3. **Add Lightning idle-resilience wrapper**  
   - Before each `.run()`, check studio status.  
   - If stopped, restart with `target.start(machine=Machine.L40S)`.  
   - Small retry loop with exponential backoff for transient failures.

4. **Smoke test**  
   - Run helper locally (once) to generate filelist.  
   - Launch training in Lightning Studio (reuse running studio if present).  
   - Verify zero HF API calls during data load (only CDN URLs).  
   - Simulate idle stop/start and confirm auto-recovery.

---

### Code snippets

#### 1) Mac-side file-list helper (`scripts/list_hf_date_folder.py`)

```python
#!/usr/bin/env python3
"""
Generate CDN filelist for a HuggingFace dataset folder.
Usage:
    python scripts/list_hf_date_folder.py \
        --repo <org/ds> \
        --date 2026-04-29 \
        --out training/filelists/2026-04-29.json
"""
import argparse
import json
import os
import time
from pathlib import Path

from huggingface_hub import HfApi, Repository

HF_API_RATE_LIMIT_RESET_BUFFER = 360  # seconds

def list_date_folder(repo_id: str, date: str, out_path: str):
    api = HfApi()
    folder_path = f"batches/mirror-merged/{date}"

    # Single non-recursive call (avoids 100x pagination)
    tree = api.list_repo_tree(repo_id=repo_id, path=folder_path, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]

    payload = {
        "repo_id": repo_id,
        "folder": folder_path,
        "date": date,
        "files": files,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "Use CDN URLs during training to bypass HF API rate limits."
    }

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")
    return payload

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List HF dataset folder for CDN training.")
    parser.add_argument("--repo", required=True, help="HF repo id, e.g. org/dataset")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    try:
        list_date_folder(args.repo, args.date, args.out)
    except Exception as e:
        # If 429, wait and retry once
        import traceback
        traceback.print_exc()
        print("Error; if 429, wait 360s and retry.")
        raise
```

#### 2) Training loader patch (`surrogate/train.py` — relevant excerpt)

```python
import json
import os
from pathlib import Path
from typing import Iterator, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import hf_hub_download

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def load_cdn_filelist(filelist_path: str) -> Dict[str, Any]:
    with open(filelist_path) as f:
        return json.load(f)

def stream_cdn_parquet_files(repo_id: str, filelist: Dict[str, Any]) -> Iterator[pa.Table]:
    """
    Yield parquet tables using CDN URLs (no HF API auth/rate-limit during training).
    Projects to {prompt, response} only.
    """
    for fname in filelist["files"]:
        if not fname.endswith(".parquet"):
            continue

        # Option A: hf_hub_download (local cache, uses CDN under the hood)
        local_path = hf_hub_download(repo_id=repo_id, filename=fname, repo_type="dataset")
        table = pq.read_table(local_path, columns=["prompt", "response"])

        # Option B: direct CDN stream (zero API calls)
        # cdn_url = CDN_TEMPLATE.format(repo=repo_id, path=fname)
        # table = pq.read_table(cdn_url, columns=["prompt", "response"])

        # Ensure schema
        if "prompt" not in table.column_names or "response" not in table.column_names:
            continue
        yield table

def build_dataset(filelist_path: str, repo_id: str) -> pa.Table:
    filelist = load_cdn_filelist(filelist_path)
    chunks = list(stream_cdn_parquet_files(repo_id, filelist))
    if not chunks:
        raise ValueError("No valid parquet files found.")
    return pa.concat_tables(chunks)
```

#### 3) Lightning idle-resilience wrapper (`surrogate/lightning_train.py`)

```python
#!/usr/bin/env python3
"""
Lightning training launcher with idle-stop recovery.
"""
import time
import traceback
from lightning import Lightning, Teamspace, Machine, Studio

LIGHTNING_MACHINE = Machine.L40S
STUDIO_NAME = "surrogate-train-studio"
MAX_RETRIES = 3
RETRY_BACKOFF = 60  # seconds

def get_or_create_studio() -> Studio:
    ts = Teamspace()
    for s in ts.studios:
        if s.name == STUDIO_NAME:
            print(f"Reusing existing studio: {s.name} ({s.status})")
            return s

    print(f"Creating studio: {STUDIO_NAME}")
    return Studio.create(
        name=STUDIO_NAME,
        machine=LIGHTNING_MACHINE,
        create_ok=True,
    )

def ensure_running(studio: Studio) -> Studio:
    if studio.status != "running":
        print(f"Studio is {studio.status}; starting...")
        studio.start(machine=LIGHTNING_MACHINE)
        # Wait for running
        for _ in range(60):
            studio.refresh()
            if studio.status == "running":
                print("Studio is running.")
                return studio
            time.sleep(10)
        raise RuntimeError("Studio failed to start.")
    return studio

def run_training_with_recovery():
    studio = get_or_create_studio()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            studio = ensure_running(studio)
            # Replace with your actual training command/script
            result = studio.run(
                [
                    "python",
                    "surrogate/train.py",
                    "--filelist",
                    "training/filelists/latest.json",
                    "--epochs",
                    "1",
                ],
               
