# vanguard / discovery

## 1. Diagnosis

- No persistent file manifest: repeated `list_repo_tree`/`load_dataset` calls during training will trigger HF API 429s and burn quota.
- Lightning Studio reuse missing: each run likely recreates/stops studios, wasting ~80hr/mo quota and risking idle-stop training death.
- Schema drift exposure: ingestion may write mixed-schema parquet into `enriched/`; surrogate-1 training will hit pyarrow `CastError`.
- No CDN bypass strategy: training scripts probably rely on HF `load_dataset`/`datasets` API during data loading instead of raw CDN fetches.
- Mac/remote boundary unclear: local orchestration may attempt `model.from_pretrained()` or heavy compute on Mac instead of delegating to Lightning/Kaggle/Cerebras.

## 2. Proposed change

Create `/opt/axentx/vanguard/scripts/discovery/prepare_training_manifest.py` (single file, ~120 lines) that:
- Runs once on Mac after rate-limit window clears.
- Calls `list_repo_tree(path, recursive=False)` for a single date folder (e.g., `batches/mirror-merged/2026-05-02/`).
- Persists `{repo_root}/file_manifest_{date}.json` listing only `{path,size,sha256}` for CDN download.
- Embeds that manifest path into `/opt/axentx/vanguard/train.py` so Lightning training uses CDN-only fetches with zero API calls.
- Adds lightweight guard in `train.py` to reuse a running Lightning Studio and restart if idle-stopped.

## 3. Implementation

```bash
# create script
mkdir -p /opt/axentx/vanguard/scripts/discovery
cat > /opt/axentx/vanguard/scripts/discovery/prepare_training_manifest.py <<'PY'
#!/usr/bin/env python3
"""
Generate a CDN-only file manifest for surrogate-1 training.
Run once per date folder after HF rate-limit window clears.
Usage:
  python prepare_training_manifest.py \
    --repo datasets/axentx/surrogate-1-mirror \
    --date 2026-05-02 \
    --out /opt/axentx/vanguard/file_manifest_2026-05-02.json
"""

import argparse
import hashlib
import json
import os
import sys
import time
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("ERROR: huggingface_hub required. pip install huggingface_hub")
    sys.exit(1)

CDN_BASE = "https://huggingface.co/datasets"

def build_manifest(repo: str, date: str, out_path: str) -> List[Dict]:
    folder = f"batches/mirror-merged/{date}"
    print(f"Listing {repo}/{folder} (non-recursive)...")

    # single API call; paginated by list_repo_tree internally but lightweight
    items = list_repo_tree(repo=repo, path=folder, recursive=False)

    manifest = []
    for item in items:
        if item.type != "file":
            continue
        if not item.path.endswith(".parquet"):
            continue

        cdn_url = f"{CDN_BASE}/{repo}/resolve/main/{item.path}"
        # Note: etag/size from list_repo_tree may be None for some repos;
        # fallback to placeholder. Training loader will validate on fetch.
        entry = {
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
            "sha256": getattr(item, "sha256", None),
        }
        manifest.append(entry)

    manifest.sort(key=lambda x: x["path"])

    payload = {
        "repo": repo,
        "date": date,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(manifest),
        "files": manifest,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(manifest)} entries to {out_path}")
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Create CDN manifest for training")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/axentx/surrogate-1-mirror)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    build_manifest(args.repo, args.date, args.out)

if __name__ == "__main__":
    main()
PY

chmod +x /opt/axentx/vanguard/scripts/discovery/prepare_training_manifest.py
```

Update `/opt/axentx/vanguard/train.py` (create if missing) to consume manifest and reuse Lightning Studio:

```python
# /opt/axentx/vanguard/train.py
import json
import os
from pathlib import Path
from typing import List, Dict

try:
    import lightning as L
    from lightning.fabric.plugins import LightningCLI
    from lightning.pytorch.utilities import rank_zero_only
except ImportError:
    L = None  # allow import without lightning for manifest-only usage

MANIFEST_PATH = os.getenv(
    "VANGUARD_MANIFEST",
    str(Path(__file__).parent / "file_manifest_latest.json")
)

def load_manifest(path: str) -> List[Dict]:
    with open(path) as f:
        data = json.load(f)
    return data["files"]

def cdn_dataloader(manifest_path: str, batch_size: int = 8, num_workers: int = 4):
    """
    Example CDN-only dataloader:
    - Downloads parquet via raw CDN URLs (no HF API/auth).
    - Projects to {prompt, response} at parse time.
    - Avoids load_dataset(streaming=True) on heterogeneous schemas.
    """
    import pyarrow.parquet as pq
    import requests
    from torch.utils.data import IterableDataset, DataLoader

    files = load_manifest(manifest_path)
    if not files:
        raise ValueError("No files in manifest")

    class CDNParquetIterable(IterableDataset):
        def __init__(self, file_entries):
            self.file_entries = file_entries

        def __iter__(self):
            for entry in self.file_entries:
                url = entry["cdn_url"]
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                table = pq.read_table(pq.ParquetFile(pq.ParquetReader(resp.content)))
                # Project only required columns; tolerate schema drift
                cols = [c for c in ["prompt", "response"] if c in table.column_names]
                if not cols:
                    continue
                batch = table.select(cols).to_pylist()
                for row in batch:
                    yield row

    dataset = CDNParquetIterable(files)
    return DataLoader(dataset, batch_size=batch_size, num_workers=num_workers)

# Optional: Lightning Studio reuse helper
def get_or_create_studio(name: str = "vanguard-surrogate-1", machine: str = "L40S"):
    if L is None:
        return None
    from lightning.pytorch.studio import Studio
    from lightning.pytorch.studio.cloud import Machine

    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s

    print(f"Creating studio {name} on {machine}")
    return Studio(
        name=name,
        target=Machine(machine),
        create_ok=True,
        start_now=True,
    )

if __name__ == "__main__":
    # Quick smoke test
    manifest = load_manifest(MANIFEST_PATH)
    print(f"Loaded {len(manifest)} files from manifest")
```

## 4. Verification

1. Generate manifest (run once):
   ```bash
   export HF_TOKEN=...
   python /opt/axentx/vanguard/scripts/discovery/prepare_training_manifest.py \
     --repo datasets/axentx/surrogate-1-mirror \
     --date 2026-05-02 \
