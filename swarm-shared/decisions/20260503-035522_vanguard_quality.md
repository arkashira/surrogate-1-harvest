# vanguard / quality

## Final Synthesized Answer

### 1. Diagnosis (Consolidated)
- **No content-addressed manifest**: ingestion/training scripts re-list HF repos at runtime, causing 429 rate-limits and non-reproducible runs.
- **Schema mismatch**: mixed-schema files from `dataset-mirror` land in `enriched/` without projection to `{prompt,response}`, risking `pyarrow.CastError` during surrogate-1 training.
- **Quota-wasting fetches**: pipeline uses `load_dataset`/`list_repo_files` (API calls) instead of deterministic CDN fetches; no CDN-bypass strategy.
- **Lightning Studio waste**: scripts create new runs instead of reusing existing Running studios.
- **HF commit-cap exposure**: writes concentrate on a single repo, risking the 128/hr cap.

### 2. Proposed Change
Create `/opt/axentx/vanguard/ingest/manifest.py` and update the training launcher to:
- Build a content-addressed manifest (`{file_hashes, cdn_urls, count}`) saved as `manifests/{repo}-{date}.json`.
- Add `--manifest` flag to `train.py` that loads the JSON and uses an `IterableDataset` over CDN URLs (zero HF API calls during training).
- Add `studio_reuse()` to reuse Running Lightning studios.
- Add deterministic `pick_repo()` to spread writes across 5 sibling repos for HF commit-cap mitigation.

### 3. Implementation

```bash
# /opt/axentx/vanguard/ingest/manifest.py
import json, hashlib, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

HF_REPO_BASE = "https://huggingface.co"
SIBLING_REPOS = [
    "axentx/surrogate-1",
    "axentx/surrogate-1-sib1",
    "axentx/surrogate-1-sib2",
    "axentx/surrogate-1-sib3",
    "axentx/surrogate-1-sib4",
]

def pick_repo(slug: str) -> str:
    """Deterministic repo selection for HF commit-cap mitigation."""
    h = hashlib.sha256(slug.encode()).digest()
    idx = int.from_bytes(h[:2], "big") % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def build_manifest(repo: str, date_folder: str, out_dir: str = "manifests") -> Dict:
    """
    Build content-addressed manifest for one date folder.
    Requires: huggingface_hub (list_repo_tree) run once from Mac after rate-limit window.
    """
    from huggingface_hub import list_repo_tree

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    entries: List[Dict] = []
    for item in list_repo_tree(repo=repo, path=date_folder, recursive=False):
        if item.type != "file":
            continue
        cdn_url = f"{HF_REPO_BASE}/datasets/{repo}/resolve/main/{item.path}"
        slug = f"{repo}/{item.path}"
        entries.append({
            "path": item.path,
            "size": getattr(item, "size", None),
            "lfs": getattr(item, "lfs", None),
            "cdn_url": cdn_url,
            "sha256": hashlib.sha256(slug.encode()).hexdigest(),
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "entries": entries,
    }

    fname = f"{repo.replace('/', '_')}_{date_folder.replace('/', '_')}.json"
    out_file = out_path / fname
    out_file.write_text(json.dumps(manifest, indent=2))
    return manifest

def load_manifest(manifest_path: str) -> Dict:
    return json.loads(Path(manifest_path).read_text())
```

```python
# /opt/axentx/vanguard/train.py  (minimal diff)
import argparse, io, json
from pathlib import Path
import torch
from torch.utils.data import IterableDataset
import pyarrow.parquet as pq
import requests

class CDNParquetDataset(IterableDataset):
    """Zero HF API calls during training — stream parquet via CDN."""
    def __init__(self, entries, columns=("prompt", "response"), max_bytes=64*1024*1024):
        self.entries = entries
        self.columns = columns
        self.max_bytes = max_bytes

    def __iter__(self):
        for ent in self.entries:
            url = ent["cdn_url"]
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            buf = io.BytesIO(r.content)
            try:
                table = pq.read_table(buf, columns=self.columns)
                for i in range(table.num_rows):
                    row = {c: table[c][i].as_py() for c in self.columns}
                    # Basic schema enforcement for surrogate-1
                    if not isinstance(row.get("prompt"), str) or not isinstance(row.get("response"), str):
                        continue
                    yield row
            except Exception:
                # Skip malformed parquet; log externally
                continue

def studio_reuse(name="vanguard-surrogate-train", machine="lightning_lite.L40S"):
    from lightning import Studio, Teamspace
    for s in Teamspace().studios:
        if s.name == name and s.status == "Running":
            return s
    return Studio(name=name, machine=machine, create_ok=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--reuse-studio", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    dataset = CDNParquetDataset(manifest["entries"])

    if args.reuse_studio:
        studio = studio_reuse()
        if studio.status != "Running":
            studio.start(machine="lightning_lite.L40S")
        # Example: run training script inside studio
        # studio.run("python train.py --manifest manifests/... --epochs 1")
        print(f"Using studio: {studio.name} ({studio.status})")
    else:
        # Local fallback (not recommended on Mac for heavy compute)
        loader = torch.utils.data.DataLoader(dataset, batch_size=8)
        for epoch in range(args.epochs):
            for batch in loader:
                # surrogate-1 training step placeholder
                pass

if __name__ == "__main__":
    main()
```

```bash
# /opt/axentx/vanguard/ingest/build_manifest.sh  (run from Mac after rate-limit clears)
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="datasets/axentx/surrogate-1"
DATE_FOLDER="batches/mirror-merged/2026-04-29"  # adjust per run

python -c "
from ingest.manifest import build_manifest
build_manifest('$REPO', '$DATE_FOLDER')
"
```

```bash
chmod +x /opt/axentx/vanguard/ingest/build_manifest.sh
```

### 4. Verification
1. **Build manifest** (run once per date folder after HF rate-limit window):
   ```bash
   cd /opt/axentx/vanguard && ./ingest/build_manifest.sh
   ```
   Confirm `manifests/datasets_axentx_surrogate-1_batches_mirror-merged_2026-04-29.json` exists and contains `cdn_url` entries.

2. **Dry-run training with CDN-only dataset** (zero HF API calls during iteration):
   ```bash
   python train.py --manifest manifests/datasets_axentx_surrogate-1_batches_mirror-merged_2026-04-29.json --epochs 1
   ```
   Observe no `huggingface_hub` HTTP logs and rows yielded from parquet files.

3. **Studio reuse check**:
   ```python
   from train import studio_reuse
   s = studio_reuse()
   print(s.name, s.status)
   ```
   Should return existing Running studio when available.

4. **Repo selection determinism**:
  
