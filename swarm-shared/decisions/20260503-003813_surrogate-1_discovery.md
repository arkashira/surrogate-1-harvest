# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs + integrity metadata, and patch Lightning training to use **zero-API CDN-only fetches** during data loading.

### Why this is highest value
- **Eliminates HF API 429 risk** during training by moving the single `list_repo_tree` call to Mac orchestration.
- **Enables Lightning training to use CDN-only fetches** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization overhead and higher rate limits.
- **Fits within 2h**: small Python script + one-line training patch + optional GitHub Actions integration.

---

### 1) Create `tools/snapshot_manifest.py` (Mac orchestration)

```python
#!/usr/bin/env python3
"""
snapshot_manifest.py
Mac-side: list one date-partition of axentx/surrogate-1-training-pairs
and emit file_manifest.json with CDN URLs + integrity metadata.

Usage:
  python tools/snapshot_manifest.py --date 2026-04-29 --out file_manifest.json
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"

def build_manifest(date_partition: str, out_path: Path) -> List[Dict]:
    """
    date_partition: e.g. "2026-04-29"
    """
    api = HfApi()
    prefix = f"{date_partition}/"

    # Single API call: list one folder (non-recursive)
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=prefix,
        repo_type="dataset",
        recursive=False,
    )

    files = [e for e in entries if e.type == "file"]
    if not files:
        print(f"No files found under {prefix}", file=sys.stderr)
        sys.exit(1)

    manifest = []
    for f in files:
        path = f.rfilename  # e.g. "2026-04-29/shard-0001.parquet"
        cdn_url = CDN_TEMPLATE.format(repo_id=REPO_ID, path=path)

        meta = {
            "path": path,
            "cdn_url": cdn_url,
            "size": getattr(f, "size", None),
            "last_modified": getattr(f, "last_modified", None),
            "sha256": None,  # populate via HEAD or LFS pointer if needed
        }
        manifest.append(meta)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fp:
        json.dump(
            {
                "repo_id": REPO_ID,
                "date_partition": date_partition,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "files": manifest,
            },
            fp,
            indent=2,
        )

    print(f"Wrote {len(manifest)} files to {out_path}")
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot HF dataset partition manifest")
    parser.add_argument("--date", required=True, help="Date partition (YYYY-MM-DD)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()

    build_manifest(date_partition=args.date, out_path=Path(args.out))

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x tools/snapshot_manifest.py
```

---

### 2) Patch Lightning training to use CDN-only fetches

Replace any `load_dataset(..., streaming=True)` or HF API-based iteration with a local manifest + CDN downloads.

```python
# train.py (or data module)
import json
import os
from pathlib import Path
from typing import Dict, Iterator

import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

MANIFEST_PATH = os.getenv("FILE_MANIFEST", "file_manifest.json")

class CDNParquetIterable(IterableDataset):
    """Stream parquet files from CDN using manifest (zero HF API calls)."""

    def __init__(self, manifest_path: str = MANIFEST_PATH, columns=None):
        super().__init__()
        self.manifest_path = manifest_path
        self.columns = columns

    def _iter_files(self) -> Iterator[Dict]:
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for meta in data["files"]:
            yield meta

    def _stream_parquet(self, cdn_url: str):
        resp = requests.get(cdn_url, stream=True, timeout=60)
        resp.raise_for_status()
        import io
        buf = io.BytesIO()
        for chunk in resp.iter_content(chunk_size=8192):
            buf.write(chunk)
        buf.seek(0)
        table = pq.read_table(buf, columns=self.columns)
        yield from table.to_pylist()

    def __iter__(self):
        for meta in self._iter_files():
            try:
                yield from self._stream_parquet(meta["cdn_url"])
            except Exception as exc:
                print(f"Skipping {meta['path']}: {exc}")
                continue
```

Usage in DataModule:
```python
train_dataset = CDNParquetIterable(columns=["prompt", "response"])
```

---

### 3) Optional: Wire into GitHub Actions (one-liner)

Add a step in your workflow (`.github/workflows/ingest.yml`) to generate the manifest before training jobs:

```yaml
- name: Generate file manifest (Mac/runner)
  run: |
    python tools/snapshot_manifest.py --date $(date +%Y-%m-%d) --out file_manifest.json
  env:
    HF_TOKEN: ${{ secrets.HF_TOKEN }}
```

Then pass `file_manifest.json` into Lightning training via env var or artifact.

---

### 4) Validation checklist (quick)

- [ ] `python tools/snapshot_manifest.py --date 2026-04-29` produces valid `file_manifest.json`.
- [ ] Training script loads manifest and streams at least one parquet via CDN without HF API calls.
- [ ] No `load_dataset(streaming=True)` or `list_repo_tree` inside training loop.
- [ ] Manifest includes `cdn_url` and `path`; training uses only CDN URLs.

---

**Estimated time**: ~90 minutes (script + training patch + smoke test).
