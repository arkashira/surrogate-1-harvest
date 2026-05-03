# vanguard / discovery

## 1. Diagnosis

- No content-addressed manifest exists for surrogate-1 date folders → training epochs drift and resumable runs are unreliable.
- Data loader resolves files via HF API (`list_repo_tree` / `load_dataset`) at runtime → exposes surrogate-1 to 429 rate limits and non-deterministic shard ordering.
- Lightning Studio reuse is not enforced → quota waste and idle-stop training loss risk.
- No CDN-only fetch path in training pipeline → unnecessary API auth checks and rate-limit exposure.
- Missing deterministic repo selection for HF commit cap mitigation (128/hr/repo) when pushing enriched parquet shards.

## 2. Proposed change

Add a manifest generator and CDN-only data loader for surrogate-1 ingestion/training:

- Create: `/opt/axentx/vanguard/surrogate1/manifest.py`
- Create: `/opt/axentx/vanguard/surrogate1/train.py` (or patch existing)
- Create: `/opt/axentx/vanguard/surrogate1/config.py`
- Update: any orchestration script that lists files to emit `manifest-{date}.json` once, then reuse.

Scope: single date folder at a time (e.g., `batches/mirror-merged/2026-04-29/`) to keep <2h.

## 3. Implementation

```bash
# Ensure structure
mkdir -p /opt/axentx/vanguard/surrogate1
```

### `/opt/axentx/vanguard/surrogate1/config.py`
```python
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
MANIFEST_DIR = BASE_DIR / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

HF_DATASET_REPO = "datasets/surrogate-1"
DATE_FOLDER = "2026-04-29"  # parameterized by orchestration
HF_CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
```

### `/opt/axentx/vanguard/surrogate1/manifest.py`
```python
import json
import hashlib
import time
from pathlib import Path
from typing import List, Dict

from huggingface_hub import list_repo_tree, hf_hub_download
from config import HF_DATASET_REPO, DATE_FOLDER, MANIFEST_DIR, HF_CDN_BASE

def build_manifest_for_date(date_folder: str = DATE_FOLDER) -> Path:
    """
    Single API call to list one date folder (non-recursive), then produce
    content-addressed manifest with CDN URLs and local deterministic shards.
    """
    # list once, non-recursive to minimize API calls
    tree = list_repo_tree(repo_id=HF_DATASET_REPO, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]

    manifest: List[Dict] = []
    for i, f in enumerate(sorted(files)):
        # CDN URL (no auth, bypasses /api/ rate limits)
        cdn_url = f"{HF_CDN_BASE}/{date_folder}/{f}"
        # deterministic shard id for resumable training
        slug = Path(f).stem
        shard_id = hashlib.sha256(f"{date_folder}/{f}".encode()).hexdigest()[:16]
        manifest.append({
            "index": i,
            "filename": f,
            "date_folder": date_folder,
            "cdn_url": cdn_url,
            "hf_path": f"{date_folder}/{f}",
            "shard_id": shard_id,
            "slug": slug,
        })

    manifest_path = MANIFEST_DIR / f"manifest-{date_folder}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path

if __name__ == "__main__":
    p = build_manifest_for_date()
    print(f"Manifest written: {p}")
```

### `/opt/axentx/vanguard/surrogate1/train.py`
```python
import json
import torch
from torch.utils.data import IterableDataset, DataLoader
from pathlib import Path
from config import MANIFEST_DIR, DATE_FOLDER, HF_CDN_BASE

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: Path):
        manifest = json.loads(manifest_path.read_text())
        self.items = manifest  # list of dicts with cdn_url

    def __iter__(self):
        for item in self.items:
            # Lightning training will run on remote; this yields URLs for remote workers
            # to fetch via CDN (zero HF API calls). Replace with actual parquet parsing
            # using pd.read_parquet(item["cdn_url"]) or pyarrow on remote workers.
            yield {
                "cdn_url": item["cdn_url"],
                "hf_path": item["hf_path"],
                "shard_id": item["shard_id"],
            }

def make_dataloader(manifest_path: Path, batch_size: int = 8, num_workers: int = 4):
    dataset = CDNParquetDataset(manifest_path)
    return DataLoader(dataset, batch_size=batch_size, num_workers=num_workers)

if __name__ == "__main__":
    # Local smoke test: generate manifest if missing, then build loader
    manifest_path = MANIFEST_DIR / f"manifest-{DATE_FOLDER}.json"
    if not manifest_path.exists():
        from manifest import build_manifest_for_date
        manifest_path = build_manifest_for_date()

    loader = make_dataloader(manifest_path, batch_size=2, num_workers=0)
    for batch in loader:
        print(batch)
        break
```

### Reuse Lightning Studio (orchestration snippet)
```python
# launcher.py  (run on Mac — orchestration only)
from lightning import Lightning, Teamspace, Machine
from pathlib import Path

teamspace = Teamspace()
studio_name = "vanguard-surrogate1-l40s"

# Reuse running studio to save quota
running = None
for s in teamspace.studios:
    if s.name == studio_name and s.status == "Running":
        running = s
        break

if running is None:
    lightning = Lightning()
    running = lightning.studio.create(
        name=studio_name,
        machine=Machine.L40S,
        script_path=str(Path(__file__).parent / "surrogate1" / "train.py"),
        create_ok=False,
    )

# Ensure not idle-stopped before run
if running.status != "Running":
    running.start(machine=Machine.L40S)

# Run training (non-blocking)
running.run()
```

## 4. Verification

1. Generate manifest (single API call):
   ```bash
   cd /opt/axentx/vanguard
   python surrogate1/manifest.py
   ```
   Confirm: `manifests/manifest-2026-04-29.json` exists and contains CDN URLs.

2. Smoke test dataloader locally:
   ```bash
   python surrogate1/train.py
   ```
   Confirm: prints one batch with `cdn_url` keys and no HF API auth errors.

3. Validate CDN-only fetch (simulate remote worker):
   ```bash
   curl -I "$(python -c "import json,pathlib; m=pathlib.Path('manifests/manifest-2026-04-29.json'); print(json.loads(m.read_text())[0]['cdn_url'])")"
   ```
   Confirm: HTTP 200 (or 302/206) — no 401/429 from HF API.

4. Lightning Studio reuse:
   - Run `launcher.py` twice; confirm second run reuses the Running studio (check logs for “reuse Running”).
   - Confirm no new studio creation on repeated runs (saves quota).

5. Determinism:
   - Delete and regenerate manifest multiple times; confirm `shard_id` and `index` remain identical for same date folder.
