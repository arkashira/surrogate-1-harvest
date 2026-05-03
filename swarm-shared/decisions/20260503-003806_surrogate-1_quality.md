# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value change**: Add Mac-side `tools/snapshot_manifest.py` that lists one date-partition with a single HF API call, emits `file_manifest.json` with CDN URLs and integrity metadata, and patch training to use zero-API CDN-only fetches during Lightning runs.

### Why this matters
- Avoids HF API 429 during long training epochs (CDN has much higher limits)
- Single API call from Mac after rate-limit window clears → deterministic file list embedded in training
- Enables studio reuse and prevents quota waste (no repeated `list_repo_files` inside training loop)

---

### 1) Create `tools/snapshot_manifest.py`

```python
#!/usr/bin/env python3
"""
snapshot_manifest.py
List one date-partition of axentx/surrogate-1-training-pairs
and emit file_manifest.json with CDN URLs + integrity metadata.

Usage:
  python tools/snapshot_manifest.py --date 2026-04-29 --out file_manifest.json
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"
CDN_ROOT = "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main"

def build_manifest(date_partition: str, out_path: Path) -> None:
    api = HfApi()

    # Single API call: non-recursive tree for the date folder
    tree = api.list_repo_tree(
        repo_id=REPO_ID,
        path=date_partition,
        recursive=False,
        repo_type="dataset",
    )

    files = [entry for entry in tree if entry.type == "file"]
    if not files:
        print(f"No files found under {date_partition}", file=sys.stderr)
        sys.exit(1)

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date_partition": date_partition,
        "repo_id": REPO_ID,
        "cdn_root": CDN_ROOT,
        "files": [],
    }

    for f in sorted(files, key=lambda x: x.path):
        # CDN URL (bypasses API auth/rate-limit during training)
        cdn_url = f"{CDN_ROOT}/{f.path}"

        # Optional: fetch sha256 via hf_hub_download metadata if needed
        # For speed we skip content hash here; training can validate on load.
        manifest["files"].append(
            {
                "path": f.path,
                "cdn_url": cdn_url,
                "size": f.size,
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snapshot HF dataset partition manifest")
    parser.add_argument("--date", required=True, help="Date partition (e.g. 2026-04-29)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()

    build_manifest(date_partition=args.date, out_path=Path(args.out))
```

Make executable:
```bash
chmod +x tools/snapshot_manifest.py
```

---

### 2) Patch training script to use CDN-only loader

Create or update `train.py` (or equivalent Lightning script) to accept `file_manifest.json` and fetch via CDN without HF API calls:

```python
# train.py (excerpt)
import json
import os
from pathlib import Path
from typing import List, Dict

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    """
    Zero-HF-API dataset loader.
    Reads file_manifest.json and streams parquet files via CDN URLs.
    """

    def __init__(self, manifest_path: str, columns=("prompt", "response")):
        super().__init__()
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files: List[Dict] = self.manifest["files"]
        self.columns = columns

    def _stream_parquet(self, cdn_url: str):
        # Stream parquet from CDN without auth/API
        with requests.get(cdn_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            # Write to temp buffer (or memory-map if small)
            data = r.content
            table = pq.read_table(pa.BufferReader(data), columns=self.columns)
            yield from table.to_pylist()

    def __iter__(self):
        for entry in self.files:
            yield from self._stream_parquet(entry["cdn_url"])

# Lightning DataModule usage
# class SurrogateDataModule(L.LightningDataModule):
#     def __init__(self, manifest_path="file_manifest.json"):
#         super().__init__()
#         self.manifest_path = manifest_path
#
#     def train_dataloader(self):
#         dataset = CDNParquetDataset(self.manifest_path)
#         return DataLoader(dataset, batch_size=...)
```

---

### 3) Update orchestration checklist (README or runbook)

Add to project README or `docs/runbook.md`:

```markdown
## Training on Lightning (CDN-only)

1. On Mac (or any dev machine), generate manifest after rate-limit window:
   ```bash
   python tools/snapshot_manifest.py --date 2026-04-29 --out file_manifest.json
   ```

2. Launch Lightning Studio (reuse running studio if available):
   ```python
   from lightning import Studio, Teamspace, Machine
   # reuse running studio to save quota
   ```

3. Run training with manifest (zero HF API calls during data load):
   ```bash
   lightning run model train.py --data.manifest_path=file_manifest.json ...
   ```
```

---

### 4) Optional: integrate into CI for automation

Add lightweight Mac runner job that generates manifest and uploads as artifact for downstream training jobs:

```yaml
# .github/workflows/snapshot.yml (optional)
name: snapshot-manifest
on:
  workflow_dispatch:
    inputs:
      date:
        required: true
jobs:
  snapshot:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install huggingface_hub pyarrow
      - run: python tools/snapshot_manifest.py --date ${{ inputs.date }} --out manifest.json
      - uses: actions/upload-artifact@v4
        with:
          name: manifest-${{ inputs.date }}
          path: manifest.json
```

---

### Time estimate
- `tools/snapshot_manifest.py`: ~20 min
- `CDNParquetDataset` loader: ~30–45 min (including tests)
- Docs/runbook updates: ~15 min
- Buffer/testing: ~30 min  
**Total ≤2h**
