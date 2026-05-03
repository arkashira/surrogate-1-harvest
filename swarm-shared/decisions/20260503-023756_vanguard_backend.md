# vanguard / backend

## 1. Diagnosis

- Backend still uses authenticated HF API (`list_repo_tree`, `load_dataset`) at runtime for data loading, burning quota and risking 429s during training/preview.
- No static file manifest exists; every run re-enumerates the repo via API instead of embedding a deterministic snapshot of file paths.
- Training script likely uses `load_dataset(streaming=True)` on heterogeneous repos, exposing surrogate-1 to pyarrow `CastError` from mixed schemas.
- No CDN-only data path; training continues to hit `/api/` endpoints instead of `https://huggingface.co/datasets/.../resolve/main/...` which bypasses auth and rate limits.
- No reuse guard for Lightning Studio; training may recreate or fail on idle-stop instead of reusing a running studio.

## 2. Proposed change

Create a backend orchestration module that:
- Generates a static file manifest (JSON) for a given date folder via a single API call (run manually from Mac).
- Embeds that manifest in the training launcher so Lightning training uses CDN-only fetches (zero API calls during data load).
- Adds a small HF CDN dataset loader that projects to `{prompt, response}` at parse time (avoids mixed-schema CastError).
- Reuses a running Lightning Studio if present; otherwise starts one (L40S priority).

Scope:
- New file: `/opt/axentx/vanguard/backend/manifest.py`
- New file: `/opt/axentx/vanguard/backend/cdn_dataset.py`
- Update: `/opt/axentx/vanguard/backend/train_launcher.py` (or create if absent)

## 3. Implementation

```bash
# Ensure backend dir exists
mkdir -p /opt/axentx/vanguard/backend
```

### `/opt/axentx/vanguard/backend/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a static file manifest for a date folder.
Run from Mac (or any dev machine) after rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

API = HfApi()
REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1")  # adjust as needed

def build_manifest(date_folder: str, out_path: Path):
    """
    List one date folder (non-recursive) and produce manifest JSON.
    Manifest format:
    {
      "repo_id": "...",
      "date_folder": "2026-05-03",
      "files": ["file1.parquet", "file2.parquet", ...],
      "generated_at": "2026-05-03T02:45:00Z"
    }
    """
    tree = API.list_repo_tree(
        repo_id=REPO_ID,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )
    files = sorted(
        [item.rfilename for item in tree if item.rfilename.endswith(".parquet")]
    )
    manifest = {
        "repo_id": REPO_ID,
        "date_folder": date_folder,
        "files": files,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path} ({len(files)} files)")
    return manifest

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python manifest.py <date_folder> [out.json]")
        sys.exit(1)
    date_folder = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(f"manifests/{date_folder}.json")
    build_manifest(date_folder, out)
```

### `/opt/axentx/vanguard/backend/cdn_dataset.py`
```python
#!/usr/bin/env python3
"""
CDN-only dataset loader for surrogate-1 training.
Uses direct HTTPS downloads (no HF API/auth) and projects to {prompt, response}.
"""
import json
import os
import tempfile
from typing import Dict, Iterator, List
from urllib.parse import quote

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_url(repo_id: str, filepath: str) -> str:
    # repo_id may be "axentx/surrogate-1"; convert to dataset URL
    return f"{CDN_ROOT}/{repo_id}/resolve/main/{quote(filepath)}"

def stream_parquet_to_rows(repo_id: str, filepath: str, max_rows: int = None) -> Iterator[Dict]:
    url = cdn_url(repo_id, filepath)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            for chunk in r.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name
    try:
        pf = pq.ParquetFile(tmp_path)
        batch_iter = pf.iter_batches(batch_size=1024)
        count = 0
        for batch in batch_iter:
            df = batch.to_pandas()
            # Project to expected surrogate-1 fields; tolerate extra columns
            for _, row in df.iterrows():
                prompt = row.get("prompt") or row.get("input") or row.get("text")
                response = row.get("response") or row.get("output") or row.get("completion")
                if prompt is None or response is None:
                    continue
                yield {"prompt": str(prompt), "response": str(response)}
                count += 1
                if max_rows is not None and count >= max_rows:
                    return
    finally:
        os.unlink(tmp_path)

def load_manifest(manifest_path: str) -> Dict:
    with open(manifest_path) as f:
        return json.load(f)

def cdn_dataset_from_manifest(manifest_path: str, max_rows: int = None) -> Iterator[Dict]:
    manifest = load_manifest(manifest_path)
    repo_id = manifest["repo_id"]
    for fname in manifest["files"]:
        yield from stream_parquet_to_rows(repo_id, fname, max_rows=max_rows)
```

### `/opt/axentx/vanguard/backend/train_launcher.py`
```python
#!/usr/bin/env python3
"""
Lightning training launcher with CDN-only data and studio reuse.
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    import lightning as L
    from lightning.fabric.plugins import LightningCLI
    from lightning.pytorch import Trainer
    from lightning.pytorch.strategies import DDPStrategy
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

# Local imports
from cdn_dataset import cdn_dataset_from_manifest

MANIFEST_PATH = os.getenv("VANGUARD_MANIFEST", "manifests/latest.json")

def find_running_studio(name: str = "vanguard-train"):
    """Reuse a running Lightning Studio to save quota."""
    try:
        from lightning.pytorch.studio import Studio, Teamspace
        studios = Teamspace.studios()
        for s in studios:
            if s.name == name and s.status == "Running":
                print(f"Reusing running studio: {s.id}")
                return s
    except Exception as e:
        print(f"Studio reuse unavailable: {e}")
    return None

def build_dataloader(manifest_path: str, batch_size: int = 8, max_rows: int = None):
    """Build an iterable dataset from CDN manifest."""
    from torch.utils.data import IterableDataset

    class CDNIterable(IterableDataset):
        def __init__(self, manifest_path, max_rows):
            self.manifest_path = manifest_path
            self.max_rows = max_rows

        def __iter__(self):
            for item in cdn_dataset_from_manifest(self.manifest_path, max_rows=self.max_rows):
                # Tokenization should happen in collate_fn/model; here we return raw text
                yield item["prompt"], item["response"]

    return torch.utils.data.DataLoader(
        CDNIterable(manifest_path, max
