# airship / discovery

## Incremental Improvement: Surrogate CDN-Only Training Manifest + Lightning Studio Reuse

**Value**: Eliminates HF API 429s during training, prevents Lightning idle-stop waste, and reduces Mac→Lightning iteration time from ~15min to <2min per cycle.

**Scope**: Add a CDN-only data loader + Lightning Studio reuse + idle-aware runner to `/opt/axentx/airship/surrogate`.

---

## Implementation Plan (≤2h)

| Step | Task | Owner | Time |
|------|------|-------|------|
| 1 | Create `scripts/build_manifest.py` — one-shot Mac script that lists a date folder via HF tree API, writes `train_manifest.json` (CDN URLs only) | Me | 15m |
| 2 | Create `surrogate/data/cdn_dataset.py` — `IterableDataset` that streams from CDN URLs (zero HF API calls) | Me | 30m |
| 3 | Create `surrogate/train/lightning_runner.py` — idempotent Studio reuse + idle-aware `.run()` wrapper | Me | 30m |
| 4 | Add `requirements-cdn.txt` (requests, pyarrow, datasets without auth extras) | Me | 5m |
| 5 | Wire into `surrogate/train/train.py` entrypoint (CLI: `--manifest`, `--studio-name`) | Me | 20m |
| 6 | Smoke test: generate manifest for one date, run 100 steps on L40S, verify no 429 and idle resume | Me | 20m |

---

## Code Snippets

### 1. `scripts/build_manifest.py` (run on Mac)

```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for Surrogate training.
Usage:
  HF_TOKEN=hf_xxx python scripts/build_manifest.py \
    --repo axentx/surrogate-data \
    --date 2026-05-01 \
    --out surrogate/train/manifests/2026-05-01.json
"""
import argparse
import json
import os
from pathlib import Path

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    # Single non-recursive call per date folder
    files = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)

    entries = []
    for f in files:
        if not f.path.endswith(".parquet"):
            continue
        entries.append(
            {
                "url": CDN_TEMPLATE.format(repo=args.repo, path=f.path),
                "path": f.path,
                "size": getattr(f, "size", None),
            }
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(entries, indent=2))
    print(f"Wrote {len(entries)} files to {out_path}")

if __name__ == "__main__":
    main()
```

---

### 2. `surrogate/data/cdn_dataset.py`

```python
from typing import Dict, Iterator, Optional
import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    """
    Streams {prompt, response} from parquet files hosted on HF CDN.
    Zero HuggingFace API calls during training.
    """

    def __init__(self, manifest_path: str, columns=("prompt", "response")):
        import json

        self.manifest = json.loads(Path(manifest_path).read_text())
        self.columns = columns

    def _stream_file(self, entry: Dict) -> Iterator[Dict]:
        url = entry["url"]
        # CDN download: no auth header, no rate-limit
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        table = pq.read_table(pq.ParquetFile(resp.content))
        for col in self.columns:
            if col not in table.column_names:
                raise KeyError(f"Missing column {col} in {url}")
        for i in range(table.num_rows):
            yield {col: table[col][i].as_py() for col in self.columns}

    def __iter__(self) -> Iterator[Dict]:
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            files = self.manifest
        else:
            files = self.manifest[worker_info.id :: worker_info.num_workers]

        for entry in files:
            yield from self._stream_file(entry)
```

---

### 3. `surrogate/train/lightning_runner.py`

```python
import time
from pathlib import Path
from lightning import LightningWork, Machine
from lightning.fabric.utilities.cloud_io import _is_running_in_cloud

class IdleAwareRunner:
    """
    Idempotent Lightning Studio reuse + idle-stop resilience.
    """

    def __init__(self, studio_name: str, machine: str = "L40S"):
        self.studio_name = studio_name
        self.machine = Machine(machine)
        self._target: Optional[LightningWork] = None

    @property
    def target(self) -> LightningWork:
        if self._target is None:
            from lightning import LightningWork

            self._target = LightningWork(
                cloud_compute=self.machine,
                cloud_build=False,
                name=self.studio_name,
            )
        return self._target

    def ensure_running(self) -> None:
        """Reuse running studio or start stopped one."""
        from lightning import Teamspace

        for s in Teamspace.studios:
            if s.name == self.studio_name:
                if s.status == "running":
                    print(f"Reusing running studio: {self.studio_name}")
                    self._target = s
                    return
                elif s.status == "stopped":
                    print(f"Restarting stopped studio: {self.studio_name}")
                    s.start(machine=self.machine)
                    self._target = s
                    return
        print(f"Creating new studio: {self.studio_name}")
        self.target.start()

    def run(
        self,
        entry_point: str,
        args: list[str],
        max_retries: int = 3,
        retry_delay: int = 360,
    ) -> None:
        """Run with idle-stop recovery and 429 backoff."""
        for attempt in range(1, max_retries + 1):
            try:
                self.ensure_running()
                self.target.run(entry_point, args)
                return
            except Exception as exc:
                if "429" in str(exc) and attempt < max_retries:
                    print(f"HF 429 hit, sleeping {retry_delay}s (attempt {attempt})")
                    time.sleep(retry_delay)
                    continue
                raise
```

---

### 4. `surrogate/train/train.py` (minimal wire-in)

```python
import argparse
from pathlib import Path

from surrogate.data.cdn_dataset import CDNParquetDataset
from surrogate.train.lightning_runner import IdleAwareRunner

def train_local(manifest: str, steps: int = 100) -> None:
    dataset = CDNParquetDataset(manifest)
    # Your existing training loop here; dataset yields only CDN rows.
    for i, batch in enumerate(dataset):
        if i >= steps:
            break
        print(f"Step {i}: prompt={batch['prompt'][:40]}...")

def train_lightning(manifest: str, studio_name: str = "surrogate-l40s") -> None:
    runner = IdleAwareRunner(studio_name=studio_name, machine="L40S")
    runner.run(
        entry_point="surrogate/train/train.py",
        args=["--manifest", manifest, "--local"],
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--studio", default="surrogate-l40s
