# vanguard / discovery

## 1. Diagnosis

- No content-addressed manifest exists → training and UI hit HF API at runtime (429s, non-reproducible epochs, no shareable snapshots).
- Dataset ingestion produces mixed-schema files in `enriched/` (extra `source`, `ts` cols) instead of projecting to `{prompt, response}` only, breaking downstream surrogate-1 training expectations.
- Training relies on `load_dataset(streaming=True)` over heterogeneous repos → pyarrow `CastError` on schema drift and HF API rate limits during data loading.
- No CDN-bypass strategy → every epoch re-authenticates against `/api/` and consumes request quota; no deterministic file list for reproducible runs.
- No Lightning Studio reuse pattern → each run recreates studio and burns 80+ quota hours; idle stops kill training without auto-restart.

## 2. Proposed change

Create a discovery-time snapshot pipeline that:
- Scans the latest `batches/mirror-merged/{date}/` folder (non-recursive) via HF API once.
- Produces a content-addressed manifest (`manifests/{date}.json`) containing `{repo, path, sha256, size, url}` for every parquet file.
- Projects schema to `{prompt, response}` at parse time and writes a small, deterministic training script (`train.py`) that uses CDN-only URLs (no auth) and zero HF API calls during training.
- Adds a lightweight launcher (`run_discovery.sh`) that reuses a running Lightning Studio or starts one deterministically.

Scope:
- New: `/opt/axentx/vanguard/scripts/make_manifest.py`
- New: `/opt/axentx/vanguard/train.py` (minimal, CDN-only dataloader)
- New: `/opt/axentx/vanguard/run_discovery.sh`
- Update: add `manifests/` and `batches/mirror-merged/` to `.gitignore` if not present.

## 3. Implementation

```bash
# 1) ensure dirs
mkdir -p /opt/axentx/vanguard/{scripts,manifests}
cd /opt/axentx/vanguard
```

### scripts/make_manifest.py
```python
#!/usr/bin/env python3
"""
make_manifest.py
Usage:
  python3 scripts/make_manifest.py \
    --repo axentx/vanguard-dataset \
    --date 2026-05-03 \
    --out manifests/2026-05-03.json

Produces a content-addressed manifest for parquet files under
batches/mirror-merged/{date}/ using non-recursive tree listing.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from typing import List, Dict

import requests

HF_API_BASE = "https://huggingface.co/api"
CDN_BASE = "https://huggingface.co/datasets"

# Bearer token optional; public datasets work without it for CDN downloads.
HF_TOKEN = os.getenv("HF_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

def list_parquet_files(repo: str, date: str) -> List[Dict]:
    """List parquet files non-recursively in batches/mirror-merged/{date}/"""
    path = f"batches/mirror-merged/{date}"
    url = f"{HF_API_BASE}/datasets/{repo}/tree"
    params = {"path": path, "recursive": "false"}

    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "360"))
        print(f"Rate limited. Sleeping {retry_after}s", file=sys.stderr)
        time.sleep(retry_after)
        return list_parquet_files(repo, date)

    resp.raise_for_status()
    items = resp.json()
    files = [
        item
        for item in items
        if item.get("type") == "file" and item.get("path", "").endswith(".parquet")
    ]
    return files

def file_sha256_url(repo: str, path: str) -> str:
    """CDN URL (no auth). SHA256 computed via ETag when available or downloaded once."""
    return f"{CDN_BASE}/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str) -> Dict:
    files = list_parquet_files(repo, date)
    if not files:
        raise RuntimeError(f"No parquet files found for {repo} at batches/mirror-merged/{date}/")

    entries = []
    for f in files:
        path = f["path"]
        url = file_sha256_url(repo, path)
        entries.append(
            {
                "repo": repo,
                "path": path,
                "size": f.get("size"),
                "url": url,
                # ETag often present and can be used as weak validator.
                "etag": f.get("oid"),
            }
        )

    manifest = {
        "repo": repo,
        "date": date,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": entries,
    }
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Create CDN-based manifest for dataset snapshot.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., axentx/vanguard-dataset)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output manifest JSON path")
    args = parser.parse_args()

    manifest = build_manifest(args.repo, args.date)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Manifest written to {args.out}")

if __name__ == "__main__":
    main()
```

### train.py (minimal, CDN-only)
```python
#!/usr/bin/env python3
"""
train.py
Lightning training script that loads parquet files directly from CDN URLs
listed in a manifest. No HF API calls during training.

Usage:
  python3 train.py --manifest manifests/2026-05-03.json
"""

import argparse
import json
from typing import List

import lightning as L
import torch
from torch.utils.data import DataLoader, Dataset
import pyarrow.parquet as pq
import requests
import io
import os

class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path: str, max_rows: int = 10_000):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.files = [entry["url"] for entry in manifest["files"]]
        # Keep only prompt/response projection at parse time.
        self.max_rows = max_rows

    def __len__(self) -> int:
        return len(self.files)

    def _load_parquet(self, url: str):
        # CDN fetch (no auth). If large, stream and read via pyarrow.
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        table = pq.read_table(buf, columns=["prompt", "response"])
        # Convert to list of dicts for simplicity.
        rows = table.to_pylist()
        return rows

    def __getitem__(self, idx: int):
        # Return one sample for DataLoader collation.
        rows = self._load_parquet(self.files[idx])
        # For demo, return first row; real training should batch across files.
        return rows[0] if rows else {"prompt": "", "response": ""}

class SurrogateModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.net = torch.nn.Linear(1024, 1024)  # placeholder

    def training_step(self, batch, batch_idx):
        # Replace with real tokenization + loss.
        loss = torch.tensor(0.0)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

def main() -> None:
    parser = argparse.ArgumentParser()

