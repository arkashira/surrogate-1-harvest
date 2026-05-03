# vanguard / quality

## Final Synthesis (Correctness + Actionability)

I merged the strongest, non-redundant insights from both candidates and resolved contradictions in favor of correctness and concrete execution.

### 1. Diagnosis (merged and prioritized)

- **No persisted manifest** → every training run re-enumerates via authenticated HF API → quota burn + 429 risk.  
  *Fix: build a deterministic JSON file list once per `(repo, dateFolder)` and reuse it.*

- **Training loader uses `load_dataset(streaming=True)` or recursive enumeration** → mixed-schema `CastError` and rate-limit exposure.  
  *Fix: replace with CDN-only fetches (`hf_hub_download` + `pyarrow`) and strict column selection.*

- **Lightning Studio reuse not enforced** → idle stop kills training and wastes quota on repeated creation.  
  *Fix: detect and reuse a running Studio; enforce deterministic machine spec and `create_ok` semantics.*

- **Data schema inconsistency** (Candidate 2): ingestion writes mixed-schema files into `enriched/` with extra columns (`source`, `ts`) instead of clean `{prompt, response}`.  
  *Fix: enforce column allowlist at load time and validate schema before training.*

- **No CDN-only fetch path** (Candidate 1): training pipeline uses authenticated API calls during data load instead of bypassing auth via CDN URLs.  
  *Fix: use `hf_hub_download` (cached) or raw CDN URLs; avoid any `datasets` API that triggers enumeration.*

---

### 2. Implementation

#### Directory setup
```bash
mkdir -p /opt/axentx/vanguard/{scripts,train,manifests}
```

#### `/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Deterministic CDN manifest builder for (repo, dateFolder).
Usage:
  HF_TOKEN=hf_... python build_manifest.py \
    --repo username/surrogate-1 \
    --date batches/mirror-merged/2026-04-29 \
    --out manifests/manifest-2026-04-29.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

HF_TOKEN = os.getenv("HF_TOKEN", "")

def build_manifest(repo_id: str, date_folder: str, out_path: Path):
    print(f"Listing {repo_id}/{date_folder} (non-recursive)...")
    try:
        tree = list_repo_tree(
            repo_id=repo_id,
            path=date_folder,
            recursive=False,
            token=HF_TOKEN or None,
        )
    except Exception as e:
        if "429" in str(e):
            print("Rate-limited. Waiting 360s...")
            time.sleep(360)
            tree = list_repo_tree(
                repo_id=repo_id,
                path=date_folder,
                recursive=False,
                token=HF_TOKEN or None,
            )
        else:
            raise

    files = sorted(
        item.rfilename for item in tree
        if item.rfilename.endswith(".parquet")
    )

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
        "count": len(files),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN file manifest.")
    parser.add_argument("--repo", required=True, help="Repo id (user/repo)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. batches/mirror-merged/YYYY-MM-DD)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()

    build_manifest(args.repo, args.date, Path(args.out))
```

#### `/opt/axentx/vanguard/train/train.py` (data loader section)
```python
import json
import os
from pathlib import Path

import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

ALLOWED_COLUMNS = {"prompt", "response"}


def load_data(manifest_path: str):
    """
    CDN-only data loader with strict schema enforcement.
    manifest_path: path to JSON built by scripts/build_manifest.py
    """
    with open(manifest_path) as f:
        manifest = json.load(f)

    repo_id = manifest["repo_id"]
    files = manifest["files"]
    if not files:
        raise ValueError("No parquet files in manifest.")

    rows = []
    for fname in files:
        local_path = hf_hub_download(
            repo_id=repo_id,
            filename=fname,
            token=os.getenv("HF_TOKEN", None),
        )
        tbl = pq.read_table(local_path, columns=list(ALLOWED_COLUMNS))
        batch = tbl.to_pylist()

        # Enforce schema: drop extra fields and reject rows missing required keys
        cleaned = []
        for item in batch:
            if not ALLOWED_COLUMNS.issubset(item.keys()):
                continue
            cleaned.append({k: item[k] for k in ALLOWED_COLUMNS})
        rows.append(cleaned)

    data = [item for batch in rows for item in batch]
    if not data:
        raise ValueError("No valid rows after schema enforcement.")
    return data
```

#### Lightning Studio reuse (corrected and actionable)
```python
from lightning.app import Studio, Teamspace
from lightning.app.utilities.packaging.cloud import _get_cloud_build

def get_or_create_studio(name: str = "surrogate-train", machine: str = "lightningai/L40S:1"):
    """
    Reuse a running Studio if present; otherwise create one.
    Avoids repeated creation that wastes quota.
    """
    running = [s for s in Teamspace.studios if s.name == name and s.status == "running"]
    if running:
        print(f"Reusing running studio: {name}")
        return running[0]

    print(f"Creating studio: {name} on {machine}")
    # Explicit spec + create_ok semantics to prevent duplicates
    return Studio(
        name=name,
        spec=_get_cloud_build(machine=machine),
        create_ok=True,
    )
```

---

### 3. Verification (concrete checklist)

1. **Build manifest once per date folder** (after HF rate-limit window clears):
   ```bash
   export HF_TOKEN=hf_...
   cd /opt/axentx/vanguard
   python scripts/build_manifest.py \
     --repo username/surrogate-1 \
     --date batches/mirror-merged/2026-04-29 \
     --out manifests/manifest-2026-04-29.json
   ```
   - Confirm: `manifests/manifest-2026-04-29.json` exists, has `"count" > 0`, and all `"files"` end in `.parquet`.

2. **Local dry-run of CDN loader**:
   ```bash
   python -c "
from train.train import load_data
data = load_data('manifests/manifest-2026-04-29.json')
print('Rows:', len(data))
print('Keys:', sorted(data[0].keys()) if data else 'empty')
"
   ```
   - Confirm: no `CastError`, no `load_dataset(streaming=True)`, rows > 0, keys exactly `['prompt', 'response']`.

3. **Studio reuse check**:
   ```python
   from lightning.app import Teamspace
   names = [s.name for s in Teamspace.studios if s.name == "surrogate-train"]
   print("Existing:", names)
   ```
   - Confirm: script lists running studio instead of creating new ones on repeated invocations.

4. **Rate-limit safety**:
   - Manifest build should perform exactly one `list_repo_tree` per date folder.
   - During training, confirm no authenticated
