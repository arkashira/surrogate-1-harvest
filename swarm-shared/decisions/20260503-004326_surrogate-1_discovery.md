# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs and a training script that uses **CDN-only** fetches (zero HF API calls during training). This implements the CDN bypass pattern and eliminates rate-limit risk during long training runs.

### Steps (est. 90 min)

1. **Create `tools/snapshot_manifest.py`** (35 min)
   - Single CLI: `python tools/snapshot_manifest.py --repo axentx/surrogate-1-training-pairs --date 2026-05-03 --out file_manifest.json`
   - Uses `huggingface_hub.list_repo_tree(path=date_folder, recursive=True)` (single API call) to capture nested parquet files.
   - Emits JSON list of objects: `{ "path": "...", "cdn_url": "https://huggingface.co/datasets/.../resolve/main/...", "size": int }`
   - Handles pagination edge-case and 429 backoff.

2. **Create `tools/train_cdn.py`** (35 min)
   - Reads `file_manifest.json`
   - Uses `torch.utils.data.IterableDataset` that downloads each file via CDN URL with `requests` (no HF API/auth)
   - Projects to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
   - Deterministic shard selection via `shard_id`/`num_shards` for multi-worker training.

3. **Update `requirements.txt`** (5 min)
   - Add `requests` if not present.

4. **Smoke test on Mac** (15 min)
   - Run snapshot for a small date folder, verify manifest, run one epoch of CDN loader.

---

## Code Snippets

### tools/snapshot_manifest.py
```python
#!/usr/bin/env python3
"""
Create CDN-only manifest for a date partition in axentx/surrogate-1-training-pairs.

Usage:
    python tools/snapshot_manifest.py --repo axentx/surrogate-1-training-pairs \
        --date 2026-05-03 --out file_manifest.json
"""

import argparse
import json
import time
import sys
from pathlib import Path
from typing import List, Dict

from huggingface_hub import list_repo_tree
from huggingface_hub.utils import HFError

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, out_path: Path) -> None:
    prefix = f"{date}/"
    entries: List[Dict] = []

    # Single API call: recursive listing to capture nested parquet files
    for entry in list_repo_tree(repo=repo, path=prefix, recursive=True):
        if entry.type != "file" or not entry.path.endswith(".parquet"):
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=entry.path)
        entries.append({
            "path": entry.path,
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
        })

    if not entries:
        print(f"[WARN] No parquet files found under prefix '{prefix}'", file=sys.stderr)

    manifest = {
        "repo": repo,
        "date": date,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": entries,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"[OK] Wrote {len(entries)} entries to {out_path}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot CDN manifest for a date partition.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()

    try:
        build_manifest(args.repo, args.date, Path(args.out))
    except HFError as e:
        print(f"[HF API error] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### tools/train_cdn.py
```python
#!/usr/bin/env python3
"""
CDN-only training data loader for surrogate-1.

Usage:
    python tools/train_cdn.py --manifest file_manifest.json \
        --shard 0 --num-shards 1 --batch-size 4
"""

import argparse
import json
import io
import sys
from pathlib import Path
from typing import Iterator, Dict, Any

import requests
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from torch.utils.data import IterableDataset, DataLoader

class CDNParquetDataset(IterableDataset):
    """Stream parquet files from CDN URLs and yield {prompt, response} pairs."""

    def __init__(self, manifest_path: Path, shard_id: int = 0, num_shards: int = 1):
        self.manifest_path = manifest_path
        self.shard_id = shard_id
        self.num_shards = num_shards
        with manifest_path.open() as f:
            self.manifest = json.load(f)
        self.files = [f for f in self.manifest["files"] if f["path"].endswith(".parquet")]
        # deterministic sharding
        self.files = [f for i, f in enumerate(self.files) if i % num_shards == shard_id]

    def _download_parquet(self, cdn_url: str) -> bytes:
        resp = requests.get(cdn_url, timeout=60)
        resp.raise_for_status()
        return resp.content

    def _iter_file(self, file_info: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        data = self._download_parquet(file_info["cdn_url"])
        table = pq.read_table(io.BytesIO(data))
        # Project only needed columns; tolerate schema variations
        cols = set(table.column_names)
        prompt_col = next((c for c in ("prompt", "input", "question") if c in cols), None)
        response_col = next((c for c in ("response", "output", "answer") if c in cols), None)

        if prompt_col is None or response_col is None:
            # fallback: use first two text-like columns
            text_cols = [c for c in cols if table.schema.field(c).type in (pa.string(), pa.large_string())]
            if len(text_cols) >= 2:
                prompt_col, response_col = text_cols[0], text_cols[1]
            else:
                return

        for i in range(table.num_rows):
            row = {
                "prompt": table.column(prompt_col)[i].as_py(),
                "response": table.column(response_col)[i].as_py(),
            }
            yield row

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for file_info in self.files:
            yield from self._iter_file(file_info)

def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-only training loader.")
    parser.add_argument("--manifest", required=True, help="Path to file_manifest.json")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-batches", type=int, default=10, help="Limit for smoke test")
    args = parser.parse_args()

    dataset = CDNParquetDataset(Path(args.manifest), shard_id=args.shard, num_shards=args.num_shards)
    loader = DataLoader(dataset, batch_size=args.batch_size)

    print(f"[INFO] Shard {args.shard}/{args.num_shards} | files={len(dataset.files)}")
    for i, batch in enumerate(loader):
        print(f"batch {i}: prompts={len(batch['prompt'])}, responses={len(batch['response'])}")
        if i >= args.max_batches - 1:

