# airship / discovery

## Implementation Plan — airship/surrogate (≤2h)

**Highest-value incremental improvement:**  
Make Surrogate training HF-rate-limit-proof and Lightning-idle-resilient by:

1. Embedding a CDN-only file list (single API call → JSON) so training uses zero HF API calls during data loading.
2. Adding Lightning Studio auto-recovery (reuse running studio, restart on idle stop) so long training jobs survive idle timeouts.

---

### Concrete steps (90 min)

| Step | Owner | Time | Command / Code |
|------|-------|------|----------------|
| 1. Generate CDN manifest (Mac) | orchestration | 10m | `python scripts/build_cdn_manifest.py --repo surrogate-datasets/mirror-merged --date 2026-05-03 --out surrogate/training/file_manifest.json` |
| 2. Add manifest to surrogate training module | surrogate | 25m | See code below (`surrogate/training/cdn_dataset.py`) |
| 3. Add Lightning auto-recovery launcher | surrogate | 25m | See code below (`surrogate/training/lightning_launcher.py`) |
| 4. Wire launcher into entrypoint | surrogate | 10m | Update `surrogate/train.py` to use launcher + manifest |
| 5. Smoke test (local dry-run) | surrogate | 10m | `python surrogate/train.py --manifest surrogate/training/file_manifest.json --dry-run` |
| 6. Deploy to Lightning (reuse or create) | surrogate | 10m | `python surrogate/training/lightning_launcher.py --run` |

---

### Code snippets

#### 1) `scripts/build_cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
Single-call HF API script to list repo folder and emit CDN manifest.
Run from Mac after rate-limit window clears.
"""
import argparse
import json
from pathlib import Path

from huggingface_hub import HfApi

def build_manifest(repo: str, date_folder: str, out_path: Path):
    api = HfApi()
    # Non-recursive per folder to minimize pagination/requests
    files = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    entries = []
    for f in files:
        if f.type != "file":
            continue
        # CDN URL bypasses API auth/rate limits
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}"
        entries.append(
            {
                "repo": repo,
                "path": f.path,
                "cdn_url": cdn_url,
                "size": getattr(f, "size", None),
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as fp:
        json.dump(entries, fp, indent=2)
    print(f"Wrote {len(entries)} entries to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="surrogate-datasets/mirror-merged")
    parser.add_argument("--date", default="2026-05-03")
    parser.add_argument("--out", default="surrogate/training/file_manifest.json")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, Path(args.out))
```

#### 2) `surrogate/training/cdn_dataset.py`

```python
import json
import torch
from torch.utils.data import IterableDataset
import aiohttp
import asyncio
from typing import List, Dict

class CDNParquetDataset(IterableDataset):
    """
    Zero HF API dataset: reads parquet shards via CDN URLs from a pre-built manifest.
    Uses streaming + selective projection to {prompt, response} only.
    """

    def __init__(self, manifest_path: str, start_idx: int = 0, end_idx: int = None):
        with open(manifest_path) as f:
            self.entries = json.load(f)[start_idx:end_idx]
        self._lock = asyncio.Lock()

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            iter_slice = self.entries
        else:
            per_worker = len(self.entries) // worker_info.num_workers
            worker_id = worker_info.id
            iter_slice = self.entries[worker_id * per_worker : (worker_id + 1) * per_worker]

        for item in iter_slice:
            # In production, use async prefetch queue; here simple sync download via requests
            import requests, io, pyarrow.parquet as pq
            resp = requests.get(item["cdn_url"], timeout=60)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            # Project only required columns; ignore heterogeneous schema
            for row in table.select(["prompt", "response"]).to_pylist():
                yield row
```

#### 3) `surrogate/training/lightning_launcher.py`

```python
#!/usr/bin/env python3
"""
Lightning Studio launcher with reuse + idle-resilient restart.
"""
import time
from pathlib import Path

from lightning_sdk import Client, Teamspace, Studio, Machine
from lightning_sdk.workspace import BuildSpec

LIGHTNING_TEAMSPACE = "surrogate-team"
STUDIO_NAME = "surrogate-trainer"
MACHINE = Machine.L40S  # Free tier falls to L40S; H200 requires lightning-lambda-prod

def get_or_create_studio(client: Client) -> Studio:
    team = Teamspace(client, LIGHTNING_TEAMSPACE)
    running = [s for s in team.studios if s.name == STUDIO_NAME and s.status == "Running"]
    if running:
        print(f"Reusing running studio: {running[0].id}")
        return running[0]

    print(f"Creating studio {STUDIO_NAME}...")
    studio = Studio(
        client,
        name=STUDIO_NAME,
        teamspace=LIGHTNING_TEAMSPACE,
        machine=MACHINE,
        build_spec=BuildSpec(
            python_version="3.10",
            packages=[
                "torch",
                "transformers",
                "datasets",
                "pyarrow",
                "aiohttp",
                "huggingface-hub",
            ],
        ),
    )
    studio.create()
    studio.start()
    return studio

def run_training_script(studio: Studio, script_path: Path, args: list[str]):
    # Ensure studio is running; restart if stopped (idle timeout)
    if studio.status != "Running":
        print(f"Studio stopped ({studio.status}). Restarting...")
        studio.start(machine=MACHINE)
        # Wait until running
        while studio.status != "Running":
            time.sleep(10)
            studio.refresh()

    # Run training; Lightning will execute inside the studio environment
    run = studio.run(
        command=["python", str(script_path)] + args,
        cwd="/workspace",
        wait=False,  # set True if you want blocking; False allows monitoring
    )
    print(f"Launched run {run.id}")
    return run

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Actually launch")
    parser.add_argument("--script", default="train.py")
    parser.add_argument("--manifest", default="training/file_manifest.json")
    args = parser.parse_args()

    client = Client()
    studio = get_or_create_studio(client)

    script_args = [
        "--manifest", args.manifest,
        "--epochs", "3",
    ]

    if args.run:
        run_training_script(studio, Path(args.script), script_args)
    else:
        print("Dry-run: would launch studio and training with args:", script_args)
```

#### 4) Update `surrogate/train.py` entrypoint (minimal change)

```python
# Near top
import argparse
from pathlib import Path
from surrogate.training.cdn_dataset import CDNParquetDataset

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="CDN manifest JSON")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    if args.dry_run
