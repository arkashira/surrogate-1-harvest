# vanguard / backend

### 1. Diagnosis (merged)
- Training scripts use authenticated HF API (`load_dataset`, `list_repo_tree`) for every run → burns quota and risks 429s.  
- No pre-listed file manifest → each run re-enumerates repo files via API instead of using a static list.  
- No CDN-bypass → data loads go through authenticated `/api/` endpoints instead of public `resolve/main/` URLs.  
- Lightning Studio reuse not enforced and idle-stop handling missing → training recreates studios and is killed by idle timeouts, wasting quota.  

### 2. Proposed change (merged)
Add a backend manifest generator + CDN-bypass data loader and studio lifecycle manager in `/opt/axentx/vanguard/backend/`:
- `manifest.py` — one-time generator (run after HF API window clears) to list files for a date folder and emit `manifest.json` with CDN URLs.  
- `data_loader.py` — Lightning-compatible `IterableDataset` that streams parquet files via CDN (zero authenticated API calls during training).  
- `studio_launcher.py` — reuse running studio, auto-restart if idle-stopped, enforce single active studio per teamspace.  

### 3. Implementation (merged and hardened)

```bash
# /opt/axentx/vanguard/backend/manifest.py
#!/usr/bin/env python3
"""
Generate manifest.json for a date folder.
Run from Mac after HF API window clears.
Usage: python manifest.py --repo org/surrogate-1 --date 2026-05-03 --out manifest.json
"""
import argparse
import json
import os
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="Folder name in repo, e.g. 2026-05-03")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    api = HfApi()
    # Single API call; recursive=False keeps it small and fast
    entries = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)

    base_cdn = f"https://huggingface.co/datasets/{args.repo}/resolve/main"
    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": [
            {
                "path": f"{args.date}/{entry.path.split('/')[-1]}",
                "cdn_url": f"{base_cdn}/{args.date}/{entry.path.split('/')[-1]}",
                "size": getattr(entry, "size", None)
            }
            for entry in entries if entry.type == "file"
        ]
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(manifest['files'])} files to {args.out}")

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/backend/data_loader.py
import json
import pyarrow.parquet as pq
import torch
from torch.utils.data import IterableDataset
import requests
import io
import time
import logging

logger = logging.getLogger(__name__)

class CDNParquetIterableDataset(IterableDataset):
    """
    Lightning-compatible dataset that streams parquet files via CDN (no HF API auth during training).
    Manifest is produced by manifest.py and committed/embedded in repo.
    """
    def __init__(self, manifest_path, batch_size=1024, columns=("prompt", "response"), max_retries=3, backoff=5):
        super().__init__()
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.files = [item["cdn_url"] for item in manifest["files"]]
        self.columns = columns
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.backoff = backoff

    def _download_parquet(self, url):
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                return pq.read_table(io.BytesIO(resp.content), columns=self.columns)
            except Exception as exc:
                if attempt == self.max_retries:
                    logger.error(f"Failed to download {url} after {self.max_retries} attempts: {exc}")
                    raise
                sleep_sec = self.backoff * (2 ** (attempt - 1))
                logger.warning(f"Retry {attempt}/{self.max_retries} for {url} in {sleep_sec}s: {exc}")
                time.sleep(sleep_sec)

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            files = self.files
        else:
            # deterministic split across workers
            per_worker = len(self.files) // worker_info.num_workers
            start = worker_info.id * per_worker
            end = start + per_worker if worker_info.id < worker_info.num_workers - 1 else len(self.files)
            files = self.files[start:end]

        for url in files:
            try:
                table = self._download_parquet(url)
                df = table.to_pandas()
                for _, row in df.iterrows():
                    yield {k: row[k] for k in self.columns}
            except Exception as exc:
                logger.error(f"Skipping {url} due to error: {exc}")
                continue
```

```python
# /opt/axentx/vanguard/backend/studio_launcher.py
import time
import logging
from lightning import Studio, L40S, Teamspace

logger = logging.getLogger(__name__)

def get_or_create_studio(name: str, teamspace: str, machine=L40S, idle_timeout_minutes=120) -> Studio:
    """
    Reuse a running studio if one exists; if idle-stopped, restart it.
    Enforces at most one active studio with `name` in the teamspace.
    """
    ts = Teamspace(teamspace)
    existing = [s for s in ts.studios if s.name == name]

    # Clean up duplicates: stop extras, keep newest running if any
    if len(existing) > 1:
        running = [s for s in existing if s.status == "running"]
        to_stop = existing if not running else [s for s in existing if s.status != "running"]
        for s in to_stop:
            logger.info(f"Stopping duplicate studio {s.id} ({s.name})")
            try:
                s.stop()
            except Exception as exc:
                logger.warning(f"Could not stop studio {s.id}: {exc}")
        # refresh after cleanup
        existing = [s for s in ts.studios if s.name == name]

    if existing:
        studio = existing[0]
        if studio.status == "running":
            logger.info(f"Reusing running studio {studio.id} ({studio.name})")
            return studio
        if studio.status == "idle":
            logger.info(f"Restarting idle-stopped studio {studio.id} ({studio.name})")
            try:
                studio.start(machine=machine)
                return studio
            except Exception as exc:
                logger.warning(f"Could not restart idle studio {studio.id}: {exc}")
                # fall through to create new
        # stopped/failed: create new (avoid reusing broken state)
        logger.info(f"Existing studio {studio.id} is {studio.status}; creating new studio")

    logger.info(f"Creating new studio {name} in teamspace {teamspace}")
    studio = Studio(
        name=name,
        teamspace=teamspace,
        machine=machine,
        idle_timeout_minutes=idle_timeout_minutes,
    )
    studio.start(machine=machine)
    return studio
```

```python
# /opt/axentx/vanguard/train.py (updated)
import os
import json
from lightning.pytorch import Trainer
from lightning.pytorch.demos.boring_classes import BoringModel
from axentx.vanguard.backend.data_loader import CDNParquetIterableDataset
from axentx.vanguard.backend.studio_launcher import get_or_create_studio

def main():
    # Reuse/create studio (prevents quota waste from repeated studio creation)
    studio = get_or_create_studio(
        name="surrogate-training",
        teamspace=os.getenv("LIGHTNING_TEAMSPACE", "default"),
    )

    # Use embedded or committed manifest to avoid HF API enumeration
