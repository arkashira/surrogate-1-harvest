# airship / frontend

## Highest-Value Incremental Improvement (<2h)

**Problem**: Surrogate-1 training blocked by HF API 429s during dataset loading; training stalls waiting on API pagination instead of GPU.

**Fix**: Implement CDN-only data loading with a pre-computed file manifest. One-time Mac-side manifest generation → Lightning training uses only CDN URLs (zero API calls during dataload).

---

## Implementation Plan (≤2h)

### 1. Create manifest generator (Mac orchestration)
- Single `list_repo_tree` call per date folder (non-recursive) → JSON list of `{path, cdn_url, size}`.
- Save to `manifests/mirror-merged/YYYY-MM-DD_files.json`.
- Commit to repo (or upload to hub as raw file) so training picks it up.

### 2. Patch training dataloader
- Load manifest JSON instead of `load_dataset`.
- Build `IterableDataset` that streams via `requests.get(cdn_url, stream=True)` + `pyarrow` projection to `{prompt, response}`.
- Zero HF API calls during training.

### 3. Reuse running Lightning Studio
- List studios; if surrogate-training is running, reuse it; else start L40S in `lightning-public-prod`.
- Pass manifest path via `Studio.run(..., inputs={"MANIFEST": manifest_path})`.

### 4. Guard against idle timeout
- Before each `.run()`, check `studio.status`; if stopped, restart with same machine.

---

## Code Snippets

### `scripts/generate_cdn_manifest.py` (Mac orchestration)
```python
#!/usr/bin/env python3
"""
Generate CDN-only manifest for a HuggingFace dataset folder.
Run from Mac after rate-limit window clears.
"""
import json
import os
from datetime import date
from pathlib import Path

from huggingface_hub import HfApi

API = HfApi()
REPO_ID = "axentx/surrogate-dataset-mirror"  # adjust
DATE_FOLDER = str(date.today())              # e.g. 2026-05-02
OUT_DIR = Path(__file__).parent.parent / "manifests" / "mirror-merged" / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "files.json"

def build_manifest() -> None:
    entries = []
    tree = API.list_repo_tree(
        repo_id=REPO_ID,
        path=f"batches/mirror-merged/{DATE_FOLDER}",
        recursive=False,
    )
    for item in tree:
        if item.type != "file":
            continue
        cdn_url = (
            f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/"
            f"batches/mirror-merged/{DATE_FOLDER}/{item.path}"
        )
        entries.append(
            {
                "path": item.path,
                "cdn_url": cdn_url,
                "size": getattr(item, "size", None),
            }
        )
    manifest = {
        "repo_id": REPO_ID,
        "date": DATE_FOLDER,
        "count": len(entries),
        "files": entries,
    }
    OUT_FILE.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {OUT_FILE} ({len(entries)} files)")

if __name__ == "__main__":
    build_manifest()
```

### `surrogate/data/cdn_dataset.py`
```python
import json
import io
from typing import Dict, Iterator

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    """Stream parquet files from CDN URLs listed in a manifest."""

    def __init__(self, manifest_path: str, columns=("prompt", "response")):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.columns = columns

    def _stream_file(self, entry: Dict) -> Iterator[Dict]:
        url = entry["cdn_url"]
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            buf = io.BytesIO()
            for chunk in r.iter_content(chunk_size=8192):
                buf.write(chunk)
            buf.seek(0)
            table = pq.read_table(buf, columns=self.columns)
            for batch in table.to_batches(max_chunksize=1024):
                df = batch.to_pydict()
                for i in range(len(df[self.columns[0]])):
                    yield {k: df[k][i] for k in self.columns}

    def __iter__(self) -> Iterator[Dict]:
        for entry in self.manifest["files"]:
            yield from self._stream_file(entry)
```

### `surrogate/train.py` (minimal patch)
```python
from pathlib import Path
from surrogate.data.cdn_dataset import CDNParquetDataset

MANIFEST = Path(os.getenv("MANIFEST", "manifests/mirror-merged/latest_files.json"))
train_ds = CDNParquetDataset(str(MANIFEST))
train_loader = DataLoader(train_ds, batch_size=8, num_workers=4)
```

### Lightning Studio reuse + idle guard
```python
from lightning import Studio, Teamspace, Machine

def get_or_start_studio(name: str = "surrogate-training-l40s"):
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    return Studio(
        create_ok=True,
        name=name,
        machine=Machine.L40S,
        cloud="lightning-public-prod",
    )

studio = get_or_start_studio()
if studio.status != "Running":
    studio.start(machine=Machine.L40S)

studio.run(
    run="surrogate/train.py",
    inputs={"MANIFEST": "manifests/mirror-merged/2026-05-02_files.json"},
)
```

---

## Verification
- Mac: `python scripts/generate_cdn_manifest.py` → produces valid JSON.
- Train: launch via Lightning Studio; observe zero HF API calls (no 429s) and GPU utilization rising immediately.
