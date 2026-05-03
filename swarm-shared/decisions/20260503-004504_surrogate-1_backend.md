# surrogate-1 / backend

## Final Unified Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs and a training script that uses **CDN-only** fetches (zero HF API during training). This eliminates 429s, keeps within HF commit-cap/rate-limit guardrails, and enables fast iteration.

---

### Steps (1h 15m total)
1. **Create tools/snapshot_manifest.py** (20m) — one API call (`list_repo_tree`), deterministic JSON manifest with CDN URLs.
2. **Create training stub lightning_train.py** (25m) — reads manifest, streams via CDN (`requests`/`urllib`), projects `{prompt,response}`, yields HF `IterableDataset` for Lightning.
3. **Update README** (10m) — usage, how to regenerate manifest, how to run training stub locally/Lightning.
4. **Smoke test** (20m) — run manifest tool, verify URLs, run 100-sample training loop.

---

### 1) tools/snapshot_manifest.py
```python
#!/usr/bin/env python3
"""
Generate CDN-only file manifest for a date-partition of axentx/surrogate-1-training-pairs.

Usage:
  python tools/snapshot_manifest.py --date 2026-04-29 --out file_manifest.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-training-pairs"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_partition(date_str: str):
    """
    Single HF API call: list top-level objects for date partition.
    Expects repo layout:
      public-merged/<date>/<files...>
    """
    api = HfApi()
    prefix = f"public-merged/{date_str}/"
    try:
        items = api.list_repo_tree(
            repo_id=REPO_ID,
            path=prefix,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        print(f"HF API error listing {prefix!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = [it.rfilename for it in items if it.type == "file"]
    if not files:
        # fallback: list parent recursively and filter
        parent = "public-merged/"
        try:
            all_items = api.list_repo_tree(
                repo_id=REPO_ID,
                path=parent,
                repo_type="dataset",
                recursive=True,
            )
            files = [it.rfilename for it in all_items if it.type == "file" and it.rfilename.startswith(prefix)]
        except Exception as exc2:
            print(f"Fallback list failed: {exc2}", file=sys.stderr)
            sys.exit(1)

    return sorted(files)

def build_manifest(date_str: str, files):
    manifest = {
        "repo_id": REPO_ID,
        "date": date_str,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": [],
    }
    for f in files:
        manifest["files"].append(
            {
                "repo_path": f,
                "cdn_url": CDN_TEMPLATE.format(repo=REPO_ID, path=f),
                "size_hint": None,  # could HEAD if needed
            }
        )
    return manifest

def main():
    parser = argparse.ArgumentParser(description="Snapshot CDN manifest for a date partition.")
    parser.add_argument("--date", required=True, help="Date partition (YYYY-MM-DD)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    parser.add_argument("--hf-token", default=os.getenv("HF_TOKEN"), help="HF token (optional for public reads)")
    args = parser.parse_args()

    print(f"Listing partition public-merged/{args.date}/ ...")
    files = list_date_partition(args.date)
    print(f"Found {len(files)} files.")

    manifest = build_manifest(args.date, files)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path}")

if __name__ == "__main__":
    main()
```

---

### 2) lightning_train.py (CDN-only training stub)
```python
#!/usr/bin/env python3
"""
Lightning-compatible training stub that uses CDN-only fetches.

Usage:
  python lightning_train.py --manifest file_manifest.json --limit 1000
"""

import argparse
import json
import sys
from itertools import islice
from typing import Iterator
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from datasets import Dataset, DatasetDict
from torch.utils.data import IterableDataset

class CDNParquetIterable(IterableDataset):
    """
    Streams parquet files from CDN URLs (no HF API auth/rate-limit).
    Projects to {prompt, response} only.
    """

    def __init__(self, cdn_urls, columns=("prompt", "response")):
        self.cdn_urls = cdn_urls
        self.columns = columns

    def __iter__(self) -> Iterator[dict]:
        for url in self.cdn_urls:
            try:
                # CDN fetch (no auth header needed for public files)
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                print(f"CDN fetch failed {url}: {exc}", file=sys.stderr)
                continue

            try:
                table = pq.read_table(pa.BufferReader(resp.content), columns=self.columns)
            except Exception as exc:
                print(f"Parquet decode failed {url}: {exc}", file=sys.stderr)
                continue

            df = table.to_pandas()
            for _, row in df.iterrows():
                # Normalize missing fields
                yield {
                    "prompt": str(row.get("prompt", "")),
                    "response": str(row.get("response", "")),
                }

def build_dataset(manifest_path, limit=None):
    manifest = json.loads(Path(manifest_path).read_text())
    urls = [f["cdn_url"] for f in manifest["files"]]
    if limit:
        urls = urls[:limit]

    iterable = CDNParquetIterable(urls, columns=("prompt", "response"))
    # Build HF Dataset from iterable (streaming-friendly)
    ds = Dataset.from_generator(
        lambda: iterable,
        features={"prompt": pa.string(), "response": pa.string()},
    )
    return DatasetDict({"train": ds})

def dummy_train_step(batch):
    # Replace with real model/tokenizer logic
    return {"loss": np.random.rand()}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="file_manifest.json")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of CDN files (for quick tests)")
    parser.add_argument("--steps", type=int, default=100, help="Training steps to run")
    args = parser.parse_args()

    print("Building CDN dataset...")
    dsets = build_dataset(args.manifest, limit=args.limit)
    train_iter = iter(dsets["train"].with_format("numpy").shuffle(seed=42).iter(batch_size=4))

    print(f"Running {args.steps} dummy steps...")
    for step in range(args.steps):
        batch = next(train_iter)
        out = dummy_train_step(batch)
        if step % 10 == 0:
            print(f"step={step} loss={out['loss']:.4f}")

    print("Done. Replace dummy_train_step with real training logic + Lightning.")

if __name__ == "__main__":
    main()
```

---

### 3) README additions (append to existing README)
```markdown
## Mac-side CDN manifest & training (recommended)

To avoid HF API rate-limits during training iterations:

1. Generate a CDN manifest (single API call):
   ```bash
   python tools/snapshot_manifest.py --date 2026-04-29 --out file_manifest.json
   ```

2. Run the
