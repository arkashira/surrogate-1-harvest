# surrogate-1 / quality

**Final Consolidated Implementation (≤2h)**

**Core principle**: One deterministic `file_manifest.json` generated on your Mac (single HF API call), then Lightning training uses **CDN-only** fetches (zero HF API calls). This removes 429s and quota pressure.

---

### 1) Create `tools/snapshot_manifest.py` (single source of truth)

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
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(date_str: str, out_path: Path):
    api = HfApi()
    prefix = f"batches/public-merged/{date_str}/"

    # Single API call: non-recursive listing for one date folder
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=prefix,
        repo_type="dataset",
        recursive=False,
    )

    files = [e for e in entries if e.type == "file" and e.path.endswith((".jsonl", ".parquet"))]
    if not files:
        print(f"No files found under {prefix}", file=sys.stderr)
        sys.exit(1)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date_partition": date_str,
        "repo_id": REPO_ID,
        "prefix": prefix,
        "strategy": "cdn-only",
        "files": [],
    }

    for f in sorted(files, key=lambda x: x.path):
        # Deterministic content-addressable token without extra calls.
        # If you want stronger integrity, add a HEAD request here once.
        path_hash = hashlib.sha256(f.path.encode()).hexdigest()[:16]
        record = {
            "path": f.path,
            "cdn_url": CDN_TEMPLATE.format(repo=REPO_ID, path=f.path),
            "size": getattr(f, "size", None),
            "etag": getattr(f, "etag", None),
            "sha256_path": path_hash,
        }
        # Prefer repo-level LFS/object sha if available
        if hasattr(f, "oid") and f.oid:
            record["sha256"] = f.oid
        manifest["files"].append(record)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {len(manifest['files'])} files -> {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snapshot HF dataset partition manifest")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD partition (e.g. 2026-04-29)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.date, Path(args.out))
```

Make executable:
```bash
chmod +x tools/snapshot_manifest.py
```

---

### 2) Add robust CDN dataset loader (`tools/cdn_dataset.py`)

```python
import json
import logging
from pathlib import Path
from typing import Dict, Iterator, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests

log = logging.getLogger(__name__)

def load_jsonl_cdn(url: str, max_lines: int | None = None) -> Iterator[Dict[str, Any]]:
    """Stream JSONL from CDN URL."""
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        for i, line in enumerate(r.iter_lines(decode_unicode=True)):
            if max_lines is not None and i >= max_lines:
                break
            if line:
                yield json.loads(line)

def load_parquet_cdn(url: str, columns=("prompt", "response")) -> Iterator[Dict[str, Any]]:
    """Download Parquet once from CDN and stream rows."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    table = pq.read_table(pa.BufferReader(resp.content), columns=columns)
    for batch in table.to_batches(max_chunksize=1024):
        rows = batch.to_pydict()
        n = len(batch)
        for k in columns:
            if len(rows[k]) != n:
                log.warning("Column length mismatch in %s", url)
        for i in range(n):
            yield {k: rows[k][i] for k in columns}

def iter_manifest_records(
    manifest_path: Path,
    max_files: int | None = None,
    max_lines_per_jsonl: int | None = None,
) -> Iterator[Dict[str, Any]]:
    """
    Yield records from manifest files via CDN.
    Skips unreadable files instead of failing the epoch.
    """
    manifest = json.loads(manifest_path.read_text())
    for i, item in enumerate(manifest["files"]):
        if max_files is not None and i >= max_files:
            break
        url = item["cdn_url"]
        try:
            if url.endswith(".jsonl"):
                yield from load_jsonl_cdn(url, max_lines=max_lines_per_jsonl)
            elif url.endswith(".parquet"):
                yield from load_parquet_cdn(url)
            else:
                log.warning("Unsupported file: %s", url)
                continue
        except Exception as exc:
            log.exception("Skipping %s: %s", url, exc)
            continue
```

---

### 3) Patch Lightning training to use CDN-only data

In your training launcher (e.g., `train.py` or notebook):

```python
from pathlib import Path
from typing import Iterator

import lightning as L
from torch.utils.data import IterableDataset, DataLoader

from tools.cdn_dataset import iter_manifest_records

class CDNIterableDataset(IterableDataset):
    def __init__(self, manifest_path: Path, max_samples: int | None = None):
        super().__init__()
        self.manifest_path = Path(manifest_path)
        self.max_samples = max_samples

    def __iter__(self) -> Iterator[Dict[str, str]]:
        count = 0
        for rec in iter_manifest_records(self.manifest_path):
            if self.max_samples is not None and count >= self.max_samples:
                break

            # Normalize to surrogate-1 schema
            prompt = rec.get("prompt") or rec.get("input") or ""
            response = rec.get("response") or rec.get("output") or ""
            if not prompt or not response:
                continue

            yield {"prompt": prompt, "response": response}
            count += 1

# Example usage in Lightning code
def run_training():
    # Reuse running Studio if available (saves quota)
    # ... existing reuse logic ...

    manifest = Path("file_manifest.json")
    if not manifest.exists():
        raise RuntimeError("Run tools/snapshot_manifest.py first")

    dataset = CDNIterableDataset(manifest, max_samples=500_000)
    loader = DataLoader(
        dataset,
        batch_size=16,
        num_workers=0,  # keep simple; increase only if CDN I/O bound
    )

    # Your model/trainer code here — zero HF API calls during data loading
    ...
```

---

### 4) Mac orchestration script

`tools/run_snapshot_and_launch.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

# 1) Snapshot one date partition (single API call from Mac)
DATE="${1:-$(date -u +%Y-%m-%d)}"
python tools/snapshot_manifest.py --date "$DATE" --out file_manifest.json

# 2) Launch or reuse Lightning Studio
python -c "
from lightning import Studio, Teamspace

studio_name = 'surrogate-1-train'
teamspace = Teamspace()
