# airship / discovery

## Implementation Plan: Manifest-Driven CDN-Only Dataset Loader for Surrogate Training

**Goal**: Eliminate HF API rate limits and mixed-schema ingestion failures in surrogate training by replacing `load_dataset`/`list_repo_files` with a **manifest-driven, CDN-only iterable loader**.

**Scope**: Surrogate AI training pipeline (`/opt/axentx/airship/surrogate/`) — data ingestion and training script.

**Estimated effort**: 1.5–2h

---

### 1. High-level approach
- **Pre-list once on Mac**: use `list_repo_tree(path, recursive=False)` per date folder (avoid recursive on big repos). Save to `file_manifest.json`.
- **Embed manifest in training script**: Lightning training does **CDN-only fetches** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with **zero API calls** during data load.
- **Schema-safe parsing**: download each file via `hf_hub_download` (or CDN) and project to `{prompt, response}` only at parse time. Do **not** use `load_dataset(streaming=True)` on heterogeneous repos.
- **No extra columns**: move attribution to filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`). Do not add `source`/`ts` cols.
- **Lightning Studio reuse**: list running studios and reuse; restart if stopped (idle timeout kills training).

---

### 2. Concrete steps (2h max)

1. **Add manifest generator** (`scripts/build_manifest.py`)
   - Input: repo, date folder
   - Output: `file_manifest.json` with `{"repo": "...", "date": "...", "files": ["path1", ...]}`

2. **Add CDN-only iterable dataset** (`surrogate/data/cdn_dataset.py`)
   - Accepts `file_manifest.json`
   - Iterates files, downloads via CDN (or `hf_hub_download` fallback)
   - Parses each file lazily and yields `{prompt, response}`

3. **Update training script** (`surrogate/train.py`)
   - Load `file_manifest.json`
   - Use `CdnDataset` with DataLoader
   - Remove any `load_dataset`/`list_repo_files` usage

4. **Lightning Studio orchestration** (`surrogate/launch_studio.py`)
   - Reuse running studio; restart if stopped
   - Use `L40S` (or `H200` only in `lightning-lambda-prod`)

5. **Cron/script hygiene** (where applicable)
   - Ensure Bash shebang (`#!/usr/bin/env bash`), `chmod +x`, invoke via `bash <script> "$@"`
   - Set `SHELL=/bin/bash` in crontab

---

### 3. Code snippets

#### `scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Build a manifest for a repo/date folder to enable CDN-only training.
Usage: python build_manifest.py --repo <repo> --date <date_folder> --out manifest.json
"""
import argparse
import json
import os
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/ds)")
    parser.add_argument("--date", required=True, help="Date folder in repo (e.g. 2026-04-29)")
    parser.add_argument("--out", default="file_manifest.json", help="Output manifest path")
    args = parser.parse_args()

    api = HfApi()
    # List non-recursive to avoid pagination explosion
    entries = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)

    files = []
    for entry in entries:
        if entry.type == "file":
            files.append(entry.path)
        elif entry.type == "directory":
            # If you want one level deeper for date folder only:
            sub_entries = api.list_repo_tree(repo_id=args.repo, path=entry.path, recursive=False)
            for sub in sub_entries:
                if sub.type == "file":
                    files.append(sub.path)

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": sorted(files)
    }

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

#### `surrogate/data/cdn_dataset.py`
```python
import json
import os
import pyarrow.parquet as pq
import pyarrow.csv as pcsv
import pyarrow as pa
from typing import Iterator, Dict, Any
from huggingface_hub import hf_hub_download
import requests
from tqdm import tqdm

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

class CdnDataset:
    """
    CDN-only iterable dataset.
    Manifest format: {"repo": "...", "date": "...", "files": ["path1", ...]}
    """
    def __init__(self, manifest_path: str, cache_dir: str = ".cache", use_cdn: bool = True):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.repo = self.manifest["repo"]
        self.files = self.manifest["files"]
        self.cache_dir = cache_dir
        self.use_cdn = use_cdn
        os.makedirs(cache_dir, exist_ok=True)

    def _download(self, path: str) -> str:
        local_path = os.path.join(self.cache_dir, path.replace("/", "_"))
        if os.path.exists(local_path):
            return local_path

        if self.use_cdn:
            url = CDN_TEMPLATE.format(repo=self.repo, path=path)
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(r.content)
        else:
            local_path = hf_hub_download(
                repo_id=self.repo,
                filename=path,
                cache_dir=self.cache_dir,
                local_dir=self.cache_dir,
                local_dir_use_symlinks=False,
            )
        return local_path

    def _parse_file(self, local_path: str) -> Iterator[Dict[str, Any]]:
        # Project to {prompt, response} only at parse time
        try:
            if local_path.endswith(".parquet"):
                tbl = pq.read_table(local_path, columns=["prompt", "response"])
            elif local_path.endswith(".csv"):
                tbl = pcsv.read_csv(local_path, convert_options=pcsv.ConvertOptions(include_columns=["prompt", "response"]))
            else:
                # fallback: try reading as arrow/ipc or skip
                return
            for batch in tbl.to_batches(max_chunksize=1000):
                df = batch.to_pydict()
                for i in range(len(df["prompt"])):
                    yield {"prompt": df["prompt"][i], "response": df["response"][i]}
        except (pa.ArrowInvalid, KeyError, Exception):
            # Skip malformed / mixed-schema files gracefully
            return

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for path in tqdm(self.files, desc="CDN files"):
            local = self._download(path)
            yield from self._parse_file(local)
```

#### `surrogate/train.py` (partial update)
```python
# Remove: from datasets import load_dataset
# Remove: api.list_repo_files(...)

from torch.utils.data import DataLoader
from surrogate.data.cdn_dataset import CdnDataset

# Load manifest generated on Mac (outside training)
dataset = CdnDataset(manifest_path="file_manifest.json", cache_dir=".cache", use_cdn=True)
loader = DataLoader(dataset, batch_size=8, num_workers=4, collate_fn=lambda x: x)

# Training loop unchanged — iterate loader
for batch in loader:
    # batch is list of {prompt, response}
    ...
```

#### `surrogate/launch_studio.py` (reuse + restart)
```python
from lightning import Studio, Machine, Teamspace

def get_or_start_studio(name: str, machine: Machine) -> Studio:
    for s in Teamspace.studios:
        if s.name == name:
            if s.status == "running":
                print(f"Reusing running studio: {name}")
                return s
            else:
                print(f"Studio {name} stopped.
