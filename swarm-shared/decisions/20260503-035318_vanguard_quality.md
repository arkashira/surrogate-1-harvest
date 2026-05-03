# vanguard / quality

# Final Synthesis (Best Parts + Correctness + Actionability)

## 1. Diagnosis (merged, tightened)

- **No content-addressed manifest**: ingestion/training re-list HF repos at runtime → 429s and non-reproducible runs.  
- **Mixed-schema files land in `enriched/` without projection to `{prompt,response}`** → `pyarrow.CastError` during surrogate-1 training.  
- **Missing CDN-only fetch strategy**: training still uses `load_dataset`/`list_repo_files` and hits HF API auth/rate limits instead of using `resolve/main/` CDN URLs.  
- **No deterministic file-list artifact**: file inventory is computed dynamically per run → training cannot be reproduced or cached.  
- **Lightning Studio reuse not enforced**: scripts likely create new studios on every run, burning 80+ quota hours/month.

## 2. Proposed change (single, minimal, high-leverage)

Add a **build-time manifest generator** + **CDN-only dataloader** and enforce **Lightning Studio reuse**:

1. `vanguard/ingest/manifest.py`  
   - `build_manifest(repo, folder, out_json)` → single non-recursive `list_repo_tree` → writes `{file, cdn_url, sha256?}`.  
2. `vanguard/train/cdn_dataset.py`  
   - `CdnDataset(file_list_json)` → streams parquet via `resolve/main/` URLs with `pyarrow.parquet.ParquetFile` and yields only `{prompt, response}`.  
3. Update training launcher to **require `--manifest`** and **reuse a running Lightning Studio** instead of creating new ones.

Scope: ~120 lines across 2 files + ~10-line patch to training entrypoint.

## 3. Implementation (corrected, production-ready)

```bash
# /opt/axentx/vanguard
mkdir -p ingest train manifests
```

### 3.1 ingest/manifest.py

```python
#!/usr/bin/env python3
"""
Build content-addressed manifest for HF dataset folder (date partition).
Usage:
  python manifest.py --repo datasets/surrogate-1 --folder 2026-05-03 --out manifests/2026-05-03.json
"""
import argparse
import json
import os
import sys
from typing import List, Dict

import huggingface_hub

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_folder_files(repo_id: str, folder: str) -> List[str]:
    """Single API call: non-recursive tree listing to avoid pagination/429."""
    tree = huggingface_hub.list_repo_tree(
        repo_id=repo_id,
        path=folder,
        repo_type="dataset",
        recursive=False,
    )
    files = [item.rfilename for item in tree if item.type == "file"]
    return files

def build_manifest(repo_id: str, folder: str, out_path: str) -> None:
    files = list_folder_files(repo_id, folder)
    entries = []
    for f in sorted(files):
        entries.append(
            {
                "file": f,
                "path": f"{folder.rstrip('/')}/{f}",
                "cdn_url": CDN_TEMPLATE.format(repo=repo_id, path=f"{folder.rstrip('/')}/{f}"),
            }
        )
    manifest = {
        "repo": repo_id,
        "folder": folder,
        "n_files": len(entries),
        "entries": entries,
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(entries)} entries to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF CDN manifest.")
    parser.add_argument("--repo", required=True, help="HF dataset repo id")
    parser.add_argument("--folder", required=True, help="Folder/date partition (e.g. batches/mirror-merged/2026-05-03)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()
    try:
        build_manifest(args.repo, args.folder, args.out)
    except huggingface_hub.utils.HfHubHTTPError as e:
        if e.response.status_code == 429:
            print("HF API rate-limited. Wait 360s and retry.", file=sys.stderr)
            sys.exit(1)
        raise
```

### 3.2 train/cdn_dataset.py

```python
#!/usr/bin/env python3
"""
CDN-only dataset loader for surrogate-1 training.
Avoids HF API calls during training; uses resolve/main/ CDN URLs.
"""
import json
from typing import Iterator, Dict
import pyarrow.parquet as pq
import requests
from io import BytesIO

try:
    import pandas as pd
    _has_pandas = True
except ImportError:
    _has_pandas = False

class CdnDataset:
    def __init__(self, manifest_path: str, columns=("prompt", "response")):
        with open(manifest_path) as fp:
            self.manifest = json.load(fp)
        self.entries = self.manifest["entries"]
        self.columns = columns

    def __len__(self) -> int:
        return len(self.entries)

    def stream_rows(self, max_files=None) -> Iterator[Dict[str, str]]:
        """Yield {prompt, response} rows from parquet files via CDN."""
        entries = self.entries if max_files is None else self.entries[:max_files]
        for ent in entries:
            url = ent["cdn_url"]
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            pf = pq.ParquetFile(BytesIO(resp.content))
            # Project only required columns to avoid mixed-schema errors
            batch = pf.read(columns=self.columns)
            if _has_pandas:
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    yield {
                        k: ("" if pd.isna(v) else str(v))
                        for k, v in row.to_dict().items()
                    }
            else:
                # fallback without pandas
                cols = batch.column_names
                for i in range(batch.num_rows):
                    row = {}
                    for k in self.columns:
                        if k in cols:
                            val = batch[k][i].as_py()
                            row[k] = "" if val is None else str(val)
                        else:
                            row[k] = ""
                    yield row

    @staticmethod
    def from_cli() -> "CdnDataset":
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--manifest", required=True)
        args = parser.parse_args()
        return CdnDataset(args.manifest)
```

### 3.3 Patch training entrypoint (example)

```python
# train/train_surrogate.py  (or wherever your launcher lives)
# Add near top:
from vanguard.train import cdn_dataset

# Replace HF loader with:
# manifest = "manifests/2026-05-03.json"
# ds = cdn_dataset.CdnDataset(manifest).stream_rows()
```

### 3.4 Lightning Studio reuse snippet (corrected)

```python
# launcher/lightning_studio.py
from lightning import Studio, Teamspace, Machine

def get_or_create_studio(name: str, machine: Machine = Machine.L40S) -> Studio:
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )
```

## 4. Verification (concrete steps)

1. **Build manifest** (run once per folder, after HF API window clears):
   ```bash
   cd /opt/axentx/vanguard
   python ingest/manifest.py --repo datasets/surrogate-1 --folder batches/mirror-merged/2026-05-03 --out manifests/2026-05-03.json
   ```
   - Confirm `manifests/2026-05-03.json` exists with `n_files > 0` and valid `cdn_url
