# vanguard / quality

# Final consolidated solution

## Diagnosis (merged)
- No persisted `(repo, dateFolder)` manifest → every training run re-enumerates via authenticated HF API → quota burn + 429 risk.
- Data loading likely uses recursive `list_repo_tree`/`load_dataset` during training, burning quota on CDN-capable files.
- No Lightning Studio reuse/idle-stop guard → unnecessary launches and quota spend.
- Cron/launch wrappers may lack shebang/executable bits → wrapper failures (pattern: opus/active-learning).
- Training dies on studio idle-stop with no auto-restart or resume.
- Data loader should use CDN-only URLs and resilient streaming for large files.

## Single concrete change set

### 1) New: manifest generator (Mac orchestration)
`/opt/axentx/vanguard/training/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a persisted manifest for (repo, dateFolder) to avoid HF API calls during training.
Usage:
    python manifest.py --repo datasets/your/repo --date 2026-05-01 --out manifest.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: huggingface_hub required. pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

CDN_BASE = "https://huggingface.co/datasets"


def build_manifest(repo: str, date_folder: str, out_path: str):
    api = HfApi()
    # Single non-recursive call per date folder (avoids pagination/rate-limit)
    items = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    files = []
    for item in items:
        if item.type != "file":
            continue
        if not item.path.lower().endswith((".jsonl", ".parquet", ".json", ".csv")):
            continue
        cdn_url = f"{CDN_BASE}/{repo}/resolve/main/{item.path}"
        files.append(
            {
                "repo": repo,
                "path": item.path,
                "cdn_url": cdn_url,
                "size": getattr(item, "size", None),
            }
        )

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "total_files": len(files),
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written to {out_path} ({len(files)} files)")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HF dataset manifest for training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/your/repo)")
    parser.add_argument("--date", required=True, help="Date folder in dataset (e.g., 2026-05-01)")
    parser.add_argument("--out", default="manifest.json", help="Output manifest path")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```
Make executable:
```bash
chmod +x /opt/axentx/vanguard/training/manifest.py
```

### 2) Update: training data loader (CDN-only, resilient)
`/opt/axentx/vanguard/training/train.py` (data-loading portion)
```python
import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset

logger = logging.getLogger(__name__)


class CDNParquetDataset(IterableDataset):
    """
    Load Parquet files from CDN URLs listed in a manifest.
    No authenticated HF API calls during training.
    """

    def __init__(self, manifest_path: str, columns=("prompt", "response"), max_retries: int = 3):
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")

        with manifest_path.open() as f:
            manifest = json.load(f)

        self.file_urls: List[str] = [f["cdn_url"] for f in manifest.get("files", [])]
        self.columns = columns
        self.max_retries = max_retries

    def _stream_parquet_rows(self, url: str) -> Iterable[Dict]:
        for attempt in range(1, self.max_retries + 1):
            try:
                pf = pq.ParquetFile(url)
                for rg_i in range(pf.metadata.num_row_groups):
                    table = pf.read_row_group(rg_i, columns=self.columns)
                    for row in table.to_pylist():
                        yield {k: row.get(k) for k in self.columns}
                return
            except Exception as exc:
                logger.warning("Attempt %s/%s failed for %s: %s", attempt, self.max_retries, url, exc)
                if attempt == self.max_retries:
                    logger.error("Skipping %s after %s attempts", url, self.max_retries)
                    return

    def __iter__(self):
        for url in self.file_urls:
            yield from self._stream_parquet_rows(url)


def get_dataloader(manifest_path: str, batch_size: int = 8, columns=("prompt", "response")):
    dataset = CDNParquetDataset(manifest_path, columns=columns)
    return torch.utils.data.DataLoader(dataset, batch_size=batch_size, num_workers=0)
```

Notes:
- Uses row-group streaming to avoid full downloads for large Parquet files.
- For JSONL/CSV, replace `_stream_parquet_rows` with `pandas.read_csv(url, chunksize=...)` or line-wise HTTP streaming.

### 3) Update: Lightning launcher with reuse + idle-stop guard
`/opt/axentx/vanguard/launch_studio.py`
```python
#!/usr/bin/env python3
"""
Launch Lightning Studio with reuse + idle-stop resilience.
"""
import time
from pathlib import Path

from lightning_sdk import Teamspace, Studio, Machine

TEAMSPACE = "vanguard"
STUDIO_NAME = "surrogate-1-train"
MANIFEST_PATH = "training/manifest.json"  # relative to studio working dir


def get_or_create_studio():
    team = Teamspace(TEAMSPACE)
    # Reuse running studio if exists
    for s in team.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s

    # Otherwise create
    print(f"Creating studio {STUDIO_NAME}")
    studio = Studio(
        name=STUDIO_NAME,
        teamspace=TEAMSPACE,
        machine=Machine.L40S,  # fallback if H200 unavailable
        # H200 available only in lightning-lambda-prod; switch if quota allows:
        # cloud="lightning-lambda-prod", machine=Machine.H200
    )
    studio.create()
    return studio


def run_training_with_guard(studio: Studio, script: str, script_args=None):
    script_args = script_args or []
    max_retries = 3
    retry_delay = 120  # seconds

    for attempt in range(1, max_retries + 1):
        if studio.status != "Running":
            print(f"Studio not running (status={studio.status}); waiting...")
            time.sleep(retry_delay)
            studio.refresh()
            continue

        try:
            run = studio.run(
                script,
                arguments=script_args,
                cwd=str(Path.cwd()),
            )
            print(f"Run started: {run.id}")
            # Wait for completion or idle-stop
            while True:
                run.refresh()
                if run.status in ("finished", "failed", "crashed"):
                    print(f"Run finished with status: {run.status}")
                    if run.status == "finished":
                        return True
                    break
                # If studio stopped (idle-stop), restart and retry
                studio.refresh()
                if studio.status != "Running":
                    print("Studio idle-stopped; restarting studio and retrying run...")
                    studio.start()
                    time
