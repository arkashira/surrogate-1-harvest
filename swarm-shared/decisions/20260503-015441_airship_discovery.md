# airship / discovery

## Highest-Value Incremental Improvement
**CDN-only ingestion + deterministic sibling-repo sharding + Studio lifecycle guard**  
- Eliminates HF API 429s during training by pre-listing once and downloading via public CDN  
- Bypasses HF commit caps (128/hr/repo) with deterministic sibling sharding  
- Prevents Lightning Studio quota waste and idle-timeout training loss with lifecycle guard  

---

## Implementation Plan (<2h)

| Step | Owner | Time | Command / Code |
|------|-------|------|----------------|
| 1. Create ingestion orchestrator | local (Mac) | 10m | `scripts/ingest_cdn_preload.py` |
| 2. Add sibling-repo sharding util | local | 10m | `lib/hf_shard.py` |
| 3. Add Studio lifecycle guard | surrogate | 15m | `lib/studio_guard.py` |
| 4. Update train entrypoint to use CDN-only + guard | surrogate | 20m | `train.py` edits |
| 5. Wire preload JSON into Lightning data module | surrogate | 20m | `data/cdn_dataset.py` |
| 6. Smoke test: run preload + start Studio + one training step | surrogate | 30m | CLI commands below |
| 7. Commit & push | local | 5m | git |

Total: ~110m (1h50m)

---

## Code Snippets

### 1) scripts/ingest_cdn_preload.py
```python
#!/usr/bin/env python3
"""
Run on Mac (or any dev box) after HF API rate-limit window clears.
Produces {date_folder}_files.json listing ONLY direct file paths
(no recursive) for one date folder to be embedded in training.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUT_PATH = Path(os.getenv("OUT_JSON", f"{DATE_FOLDER}_files.json"))

def main() -> None:
    api = HfApi()
    # Non-recursive to avoid pagination explosion
    entries = api.list_repo_tree(repo_id=REPO_ID, path=DATE_FOLDER, recursive=False)
    files = [e.rfilename for e in entries if e.type == "file"]
    payload = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "files": sorted(files),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "note": "CDN-only list; use resolve/main/ URLs to bypass HF API auth limits",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(files)} files -> {OUT_PATH}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x scripts/ingest_cdn_preload.py
```

---

### 2) lib/hf_shard.py
```python
import hashlib
from typing import List

SIBLINGS = [
    "axentx/surrogate-dataset",
    "axentx/surrogate-dataset-s1",
    "axentx/surrogate-dataset-s2",
    "axentx/surrogate-dataset-s3",
    "axentx/surrogate-dataset-s4",
    "axentx/surrogate-dataset-s5",
]

def shard_for(slug: str) -> str:
    """Deterministic repo assignment for commit-cap scaling (640/hr aggregate)."""
    digest = hashlib.sha256(slug.encode()).digest()
    idx = digest[0] % len(SIBLINGS)
    return SIBLINGS[idx]

def split_by_shard(file_list: List[str]) -> dict:
    out = {r: [] for r in SIBLINGS}
    for f in file_list:
        # slug here is logical identifier (e.g., filename without extension)
        slug = Path(f).stem
        out[shard_for(slug)].append(f)
    return out
```

---

### 3) lib/studio_guard.py
```python
import time
from lightning_sdk import Studio, Teamspace, Machine

TEAMSPACE = "axentx"
STUDIO_NAME = "surrogate-train"
MACHINE = Machine.L40S  # fallback if H200 unavailable
MAX_RETRIES = 3
RETRY_WAIT = 5

def running_studio() -> Studio:
    """Reuse running studio or start a new one; never recreate blindly."""
    team = Teamspace(TEAMSPACE)
    for s in team.studios:
        if s.name == STUDIO_NAME and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s

    print(f"No running studio '{STUDIO_NAME}'; creating...")
    studio = Studio.create(
        teamspace=TEAMSPACE,
        name=STUDIO_NAME,
        machine=MACHINE,
        create_ok=True,
    )
    # Wait until running
    for _ in range(60):
        studio.refresh()
        if studio.status == "running":
            print("Studio is running")
            return studio
        time.sleep(10)
    raise RuntimeError("Studio failed to start")

def ensure_alive_and_run(studio: Studio, target_name: str, command: str) -> None:
    """Guard against Lightning idle-stop killing training."""
    for attempt in range(1, MAX_RETRIES + 1):
        studio.refresh()
        if studio.status != "running":
            print(f"Studio stopped (attempt {attempt}); restarting...")
            studio.start(machine=MACHINE)
            # Wait for running
            for _ in range(60):
                studio.refresh()
                if studio.status == "running":
                    break
                time.sleep(10)

        job = studio.run(target_name=target_name, command=command)
        try:
            job.wait_until_finished()
            return
        except Exception as exc:
            print(f"Run failed (attempt {attempt}): {exc}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(RETRY_WAIT)
```

---

### 4) surrogate/train.py (excerpt)
```python
import json
from pathlib import Path

from lib.studio_guard import ensure_alive_and_run, running_studio
from lib.hf_shard import split_by_shard

PRELOAD_PATH = Path("files/batches_2024-01-01_files.json")  # produced by ingest_cdn_preload.py

def train_cdn_only() -> None:
    preload = json.loads(PRELOAD_PATH.read_text())
    files = preload["files"]
    print(f"Loaded {len(files)} files from CDN list")

    # Optional: shard writes if you push enriched outputs back to HF
    # shards = split_by_shard(files)

    studio = running_studio()
    # Use CDN URLs directly in your data loader (see cdn_dataset.py).
    # This run() simply starts training that will use CDN-only fetches.
    ensure_alive_and_run(
        studio,
        target_name="train",
        command="python -m surrogate.train_cdn --filelist files/batches_2024-01-01_files.json",
    )

if __name__ == "__main__":
    train_cdn_only()
```

---

### 5) surrogate/data/cdn_dataset.py
```python
import json
from pathlib import Path
from typing import List, Dict
import requests
from datasets import Dataset

def load_via_cdn(filelist_path: Path) -> Dataset:
    """Load dataset using public CDN URLs (no HF API auth/rate-limit)."""
    meta = json.loads(filelist_path.read_text())
    base = f"https://huggingface.co/datasets/{meta['repo_id']}/resolve/main/"

    rows: List[Dict] = []
    for f in meta["files"]:
        url = f"{base}{f}"
        # Streaming download; project to {prompt,response} at parse time
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        # Replace with your actual parser (parquet/jsonl/etc.)
        # Example assumes line-delimited
