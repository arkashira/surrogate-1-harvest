# airship / frontend

## Final Implementation (synthesized, contradiction-resolved, action-ready)

**Chosen scope (≤2h):**  
Make Surrogate training **HF-rate-limit-proof** and **Lightning-idle-resilient** by embedding a CDN-only file list and adding auto-recovery for idle timeouts.

---

### Why this ships value in <2h
- Eliminates HF API rate-limit failures during training (CDN-only fetches).
- Prevents wasted Lightning quota when idle timeouts kill long-running jobs.
- Small, focused changes: one file-list generator + one training loader patch + one idle-resume guard.

---

## Concrete Implementation

### 1) Generate CDN-only file list (run once on Mac orchestration node)

`scripts/generate_cdn_filelist.py`
```python
#!/usr/bin/env python3
"""
Generate CDN file list for a date folder to embed in training.
Usage:
    python scripts/generate_cdn_filelist.py \
        --repo datasets/your-org/surrogate-mirror \
        --date 2026-04-29 \
        --out surrogate/training/filelist_2026-04-29.json
"""
import argparse
import json
import os
import time
from huggingface_hub import list_repo_tree

HF_TOKEN = os.getenv("HF_TOKEN")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    # single non-recursive call per date folder (avoids 100x pagination)
    folder = f"batches/mirror-merged/{args.date}"
    retries = 3
    for attempt in range(retries):
        try:
            tree = list_repo_tree(
                repo_id=args.repo,
                path=folder,
                recursive=False,
                token=HF_TOKEN,
            )
            break
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = 360
            print(f"list_repo_tree failed ({e}), waiting {wait}s before retry...")
            time.sleep(wait)

    files = [
        f"{folder}/{entry.path.split('/')[-1]}"
        for entry in tree
        if entry.type == "file" and entry.path.lower().endswith(".parquet")
    ]

    payload = {
        "repo": args.repo,
        "date": args.date,
        "files": sorted(files),
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x scripts/generate_cdn_filelist.py
```

---

### 2) CDN-only dataset loader (training script)

`surrogate/training/cdn_dataset.py`
```python
import json
import os
from typing import Iterator, Tuple

import pyarrow.parquet as pq
import requests
from datasets import Dataset
from torch.utils.data import IterableDataset


class CDNParquetDataset(IterableDataset):
    """
    Loads Parquet shards via HuggingFace CDN (no Authorization header).
    Expects filelist JSON produced by generate_cdn_filelist.py.
    Projects each row to {prompt, response} at parse time.
    """

    CDN_BASE = "https://huggingface.co/datasets"

    def __init__(self, filelist_path: str, repo: str | None = None):
        with open(filelist_path) as f:
            meta = json.load(f)
        self.repo = repo or meta["repo"]
        self.files = meta["files"]

    def _cdn_url(self, path: str) -> str:
        # resolve/main bypasses API auth and rate limits
        return f"{self.CDN_BASE}/{self.repo}/resolve/main/{path}"

    def _stream_shard(self, path: str) -> Iterator[Tuple[str, str]]:
        url = self._cdn_url(path)
        # stream download to avoid large memory spikes
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open("/tmp/shard.parquet", "wb") as tmp:
                for chunk in r.iter_content(chunk_size=8192):
                    tmp.write(chunk)

        table = pq.read_table("/tmp/shard.parquet")
        # project only what we need; attribution lives in filename pattern
        for i in range(table.num_rows):
            row = table.slice(i, 1).to_pydict()
            prompt = row.get("prompt", [None])[0]
            response = row.get("response", [None])[0]
            if prompt is not None and response is not None:
                yield prompt, response

    def __iter__(self) -> Iterator[dict]:
        for path in self.files:
            try:
                for prompt, response in self._stream_shard(path):
                    yield {"prompt": prompt, "response": response}
            except Exception as exc:
                # skip corrupt shard but keep training alive
                print(f"Skipping {path}: {exc}")
                continue


def to_datasets(dataset: CDNParquetDataset) -> Dataset:
    return Dataset.from_generator(lambda: iter(dataset))
```

---

### 3) Lightning idle-resume guard (orchestration wrapper)

`surrogate/training/run_with_resume.py`
```python
#!/usr/bin/env python3
"""
Lightning-aware training launcher with idle-timeout recovery.
Checks studio status before each run and restarts if stopped.
"""
import time
import os
import sys

from lightning import Fabric, LightningWork, LightningFlow, LightningApp
from lightning.app import LightningStudio

# Example training function (replace with your real train step)
def train_step(fabric: Fabric):
    # Your training loop here; this is a stub
    fabric.print("Running training step...")
    time.sleep(10)


class SurrogateTrainer(LightningWork):
    def __init__(self, filelist_path: str):
        super().__init__()
        self.filelist_path = filelist_path

    def run(self):
        from surrogate.training.cdn_dataset import CDNParquetDataset, to_datasets
        from torch.utils.data import DataLoader

        fabric = Fabric()
        dataset = CDNParquetDataset(self.filelist_path)
        ds = to_datasets(dataset)
        loader = DataLoader(ds, batch_size=8, shuffle=True)
        loader = fabric.setup_dataloaders(loader)

        for epoch in range(3):  # example
            fabric.print(f"Epoch {epoch}")
            for batch in loader:
                # dummy train step
                fabric.all_gather(batch["prompt"])
                train_step(fabric)


class SurrogateFlow(LightningFlow):
    def __init__(self, filelist_path: str):
        super().__init__()
        self.filelist_path = filelist_path
        self.trainer = SurrogateTrainer(filelist_path)

    def run(self):
        # Idle-stop recovery: if trainer stopped, restart it
        studio = LightningStudio(name="surrogate-training", create_ok=True)
        if studio.status != "running":
            self.trainer.start(machine="lightning-lambda-prod:L40S")
        self.trainer.run()


if __name__ == "__main__":
    filelist = os.getenv("CDN_FILELIST", "surrogate/training/filelist_latest.json")
    if not os.path.exists(filelist):
        print(f"Filelist not found: {filelist}")
        sys.exit(1)

    app = LightningApp(SurrogateFlow(filelist))
```

---

### 4) Cron / orchestration snippet (set SHELL)

`crontab -e` (on Mac orchestration node)
```bash
SHELL=/bin/bash
# Generate filelist nightly after mirror ingestion completes
0 3 * * * /bin/bash /opt/axentx/airship/scripts/generate_cdn_filelist.py --repo datasets/your-org/surrogate-mirror --date $(date -I) --out /opt/axentx/airship/surrogate/training/filelist_$(date -I).json >> /var/log/airship
