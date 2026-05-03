# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs, and a training script that uses **CDN-only** fetches (zero HF API calls during data load). This implements the CDN bypass pattern and prevents 429s while training on Lightning.

### Steps (1h 30m total)

1. **Create tools/snapshot_manifest.py** (30m)  
   - Single `list_repo_tree(path=date_partition, recursive=True)` call  
   - Filter to parquet/jsonl only  
   - Emit `file_manifest.json`: `{"date": "...", "files": [{"path": "...", "cdn_url": "...", "size": ...}]}`  
   - Save to `manifests/YYYY-MM-DD.json`

2. **Add lightweight CDN fetcher module** (30m)  
   - `surrogate_1/data/cdn_loader.py` with `IterableDataset` that reads manifest and streams via `requests.get(cdn_url, stream=True)` + `pyarrow.parquet` or `jsonl` line reader  
   - Zero `datasets.load_dataset` and zero HF API auth during training  
   - Deterministic shard/batch logic for multi-GPU

3. **Add example training script** (30m)  
   - `train_cdn.py` that takes `--manifest` arg, builds dataset from CDN URLs, runs a small dummy training loop (or real surrogate-1 step)  
   - Validate no HF API calls via logging

4. **Update README / usage note** (15m)  
   - One-line to generate manifest on Mac, copy to Lightning, run training

5. **Test run** (15m)  
   - Generate manifest for a small date partition, run `train_cdn.py` locally (or on Lightning via Studio) and confirm zero HF API traffic

---

### 1. tools/snapshot_manifest.py

```python
#!/usr/bin/env python3
"""
Generate a CDN-only file manifest for a date partition in
axentx/surrogate-1-training-pairs.

Usage:
    python tools/snapshot_manifest.py --date 2026-04-29 --out manifests/
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_url


REPO_ID = "axentx/surrogate-1-training-pairs"


def build_manifest(date_partition: str, out_dir: Path):
    api = HfApi()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Single API call: list tree for this date partition (non-recursive=False => recursive)
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=date_partition,
        repo_type="dataset",
        recursive=True,
    )

    files = []
    for e in entries:
        if e.type != "file":
            continue
        if not (e.path.endswith(".parquet") or e.path.endswith(".jsonl")):
            continue

        cdn_url = hf_hub_url(
            repo_id=REPO_ID,
            filename=e.path,
            repo_type="dataset",
        )

        files.append({
            "path": e.path,
            "cdn_url": cdn_url,
            "size": e.size if hasattr(e, "size") else None,
        })

    manifest = {
        "date": date_partition,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "repo": REPO_ID,
        "n_files": len(files),
        "files": files,
    }

    out_path = out_dir / f"{date_partition}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path} ({len(files)} files)")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN manifest for a date partition")
    parser.add_argument("--date", required=True, help="Date partition (e.g. 2026-04-29)")
    parser.add_argument("--out", default="manifests", help="Output directory")
    args = parser.parse_args()

    build_manifest(date_partition=args.date, out_dir=Path(args.out))
```

---

### 2. surrogate_1/data/cdn_loader.py

```python
import json
import pyarrow.parquet as pq
import pyarrow as pa
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader
from typing import List, Dict, Optional
import io


class CDNParquetIterable(IterableDataset):
    """
    Stream parquet files from CDN URLs listed in a manifest.
    Yields raw rows as dicts (or project to {prompt, response} downstream).
    """

    def __init__(self, manifest_path: str, columns: Optional[List[str]] = None, start_offset: int = 0, end_offset: Optional[int] = None):
        with open(manifest_path) as f:
            manifest = json.load(f)

        self.files = manifest["files"]
        if start_offset or end_offset:
            self.files = self.files[start_offset:end_offset]

        self.columns = columns
        self.manifest_date = manifest.get("date")

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            # single-process
            files = self.files
        else:
            # shard across workers
            per_worker = len(self.files) // worker_info.num_workers
            files = self.files[worker_info.id * per_worker : (worker_info.id + 1) * per_worker]

        for item in files:
            url = item["cdn_url"]
            try:
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    buf = io.BytesIO(r.content)
                    table = pq.read_table(buf, columns=self.columns)
                    for batch in table.to_batches(max_chunksize=1024):
                        for row in batch.to_pylist():
                            yield row
            except Exception as exc:
                # log and continue; don't kill entire epoch on one bad file
                print(f"[cdn_loader] failed {url}: {exc}")
                continue


# Convenience helper for non-torch usage
def stream_rows_from_manifest(manifest_path: str, columns=None):
    loader = CDNParquetIterable(manifest_path=manifest_path, columns=columns)
    yield from iter(loader)
```

---

### 3. train_cdn.py (minimal example)

```python
#!/usr/bin/env python3
"""
Example CDN-only training loop (dummy step).
Run:
    python train_cdn.py --manifest manifests/2026-04-29.json
"""

import argparse
import json
import torch
from torch.utils.data import DataLoader
from surrogate_1.data.cdn_loader import CDNParquetIterable


def dummy_collate(batch):
    # Expect rows to contain 'prompt' and 'response' (adjust as needed)
    return batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--columns", default="prompt,response", help="comma-separated columns to project")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--steps", type=int, default=10)
    args = parser.parse_args()

    columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    dataset = CDNParquetIterable(
        manifest_path=args.manifest,
        columns=columns or None,
    )
    loader = DataLoader(dataset, batch_size=args.batch_size, collate_fn=dummy_collate, num_workers=0)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    step = 0
    for batch in loader:
        if step >= args.steps:
            break
        # Replace with real surrogate-1 training step
        print(f"[step {step}] batch size={len(batch)} sample={batch[0] if batch else None}")
        step += 1

    print("CDN-only training loop complete")


if __name__ == "__main__":
    main()
```
