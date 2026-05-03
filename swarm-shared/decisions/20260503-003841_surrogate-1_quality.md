# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs + integrity metadata, and patch Lightning training to use **zero-API CDN-only fetches** during training. This eliminates HF API rate-limit risk during long training runs and preserves quota for ingestion orchestration.

### Steps (1h 45m total)

1. **Create `tools/snapshot_manifest.py`** (30m)  
   - Single `list_repo_tree(path=date_partition, recursive=True)` call from Mac after rate-limit window.  
   - Filter to parquet/jsonl files.  
   - Emit `file_manifest.json`: `{date, repo, files: [{path, cdn_url, size, md5?}]}`.  
   - CDN URL pattern: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.

2. **Add `tools/requirements-dev.txt`** (5m)  
   - `huggingface_hub`, `requests`, `tqdm`.

3. **Patch Lightning training script** (45m)  
   - Accept `--manifest file_manifest.json` (or env).  
   - Use a custom `IterableDataset` that streams from CDN URLs via `requests`/`urllib` with range reads and `pyarrow`/`parquet` projection to `{prompt, response}`.  
   - Zero `huggingface_hub` API calls during training.  
   - Add retry/backoff for CDN 429s (separate, higher limit).  
   - Preserve shuffle via reservoir or deterministic shard order + per-file shuffle.

4. **Update `bin/dataset-enrich.sh`** (10m)  
   - Add optional `--write-manifest` flag to produce `file_manifest.json` alongside enriched outputs for local dev.

5. **Add README section** (10m)  
   - Usage: `python tools/snapshot_manifest.py --repo axentx/surrogate-1-training-pairs --date 2026-05-03 --out manifest.json`.  
   - Training: `lightning run model train.py --manifest manifest.json`.

6. **Smoke test** (15m)  
   - Run manifest tool on Mac.  
   - Run one mini-epoch of training with manifest on Lightning L40S (Studio reuse).

---

## Code Snippets

### tools/snapshot_manifest.py
```python
#!/usr/bin/env python3
"""
Create CDN-only manifest for a date partition in a HuggingFace dataset repo.
Usage:
    python tools/snapshot_manifest.py \
        --repo axentx/surrogate-1-training-pairs \
        --date 2026-05-03 \
        --out file_manifest.json
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi, Repository

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_partition: str, out_path: str) -> None:
    api = HfApi()
    # Single API call: list tree for the date folder only
    entries = api.list_repo_tree(repo=repo, path=date_partition, recursive=True)

    files = []
    for e in entries:
        if e.type != "file":
            continue
        if not (e.path.endswith(".parquet") or e.path.endswith(".jsonl")):
            continue
        files.append({
            "path": e.path,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=e.path),
            "size": e.size,
        })

    manifest = {
        "repo": repo,
        "date_partition": date_partition,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    Path(out_path).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote manifest with {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN manifest for HF dataset date partition.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. axentx/surrogate-1-training-pairs)")
    parser.add_argument("--date", required=True, help="Date partition path in repo (e.g. batches/public-merged/2026-05-03)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()
    try:
        build_manifest(args.repo, args.date, args.out)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
```

### tools/requirements-dev.txt
```
huggingface_hub>=0.22.0
requests>=2.31.0
tqdm>=4.66.0
pyarrow>=14.0.0
```

### training/cdn_dataset.py (minimal)
```python
import json
import io
import pyarrow.parquet as pq
import numpy as np
import requests
from torch.utils.data import IterableDataset
from typing import List, Dict

class CDNParquetIterable(IterableDataset):
    def __init__(self, manifest_path: str, columns=("prompt", "response"), buffer_size: int = 8 * 1024 * 1024):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.files = [f["cdn_url"] for f in manifest["files"]]
        self.columns = columns
        self.buffer_size = buffer_size

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        files = self.files
        if worker_info is not None:
            # deterministic per-worker shard
            per_worker = len(files) // worker_info.num_workers
            files = files[worker_info.id * per_worker : (worker_info.id + 1) * per_worker]

        for url in files:
            # Stream parquet from CDN; pyarrow can read from file-like
            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            buf = io.BytesIO()
            for chunk in resp.iter_content(chunk_size=self.buffer_size):
                buf.write(chunk)
            buf.seek(0)
            table = pq.read_table(buf, columns=self.columns)
            df = table.to_pandas()
            # deterministic per-file shuffle
            df = df.sample(frac=1.0, random_state=hash(url) & 0xFFFFFFFF).reset_index(drop=True)
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"]}
```

### Patch to Lightning train.py (snippet)
```python
import argparse
from cdn_dataset import CDNParquetIterable
from torch.utils.data import DataLoader

parser = argparse.ArgumentParser()
parser.add_argument("--manifest", required=True, help="Path to file_manifest.json")
parser.add_argument("--batch-size", type=int, default=8)
args = parser.parse_args()

dataset = CDNParquetIterable(args.manifest)
loader = DataLoader(dataset, batch_size=args.batch_size, num_workers=4)

# Use loader in Lightning training step — zero HF API calls during epoch.
```

### bin/dataset-enrich.sh (optional manifest flag)
```bash
# Add near top
WRITE_MANIFEST=false
while [[ $# -gt 0 ]]; do
  case $1 in
    --write-manifest) WRITE_MANIFEST=true; shift ;;
    *) break ;;
  esac
done

# After enrichment loop, if WRITE_MANIFEST=true, produce manifest.json for date
if $WRITE_MANIFEST; then
  python tools/snapshot_manifest.py --repo "$REPO" --date "$DATE_PARTITION" --out "manifest_${DATE_PARTITION}.json"
fi
```

### README addition
```markdown
## CDN-only training (recommended)

To avoid HF API rate limits during long training runs:

1. Generate manifest on Mac (after rate-limit window clears):
   ```bash
   python tools/snapshot_manifest.py \
     --repo axentx/surrogate-1-training-pairs \
     --date batches/public-merged/2026-05-03 \
     --out manifest.json
   ```

2. Launch Lightning training with manifest:
  
