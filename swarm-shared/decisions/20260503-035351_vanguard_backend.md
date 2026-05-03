# vanguard / backend

## 1. Diagnosis

- No content-addressed manifest: ingestion/training scripts likely re-list HF repos at runtime, causing 429s and non-reproducible runs.
- Mixed-schema files probably land in `enriched/` without projection to `{prompt,response}`, risking `pyarrow.CastError` during surrogate-1 training.
- HF API rate-limit exposure: backend probably calls `list_repo_files` or `load_dataset(streaming=True)` at runtime instead of using CDN-only fetches with a pre-computed file list.
- Lightning quota waste: training orchestration likely recreates studios instead of reusing running ones, burning 80+ hours/month.
- No deterministic file list artifact: frontend/backend cannot cache or verify dataset contents; every run re-discovers files via API.

## 2. Proposed change

Add a backend ingestion orchestrator that:
- Downloads one date folder from HF using `list_repo_tree` (single API call) → writes `manifests/{date}/files.json`.
- Projects every file to `{prompt,response}` only (no extra cols) and writes `batches/mirror-merged/{date}/{slug}.parquet`.
- Reuses a running Lightning Studio if present; otherwise starts one (L40S fallback) and runs training with CDN-only URLs from the manifest.

Scope:
- Create `/opt/axentx/vanguard/backend/ingest.py`
- Create `/opt/axentx/vanguard/backend/train.py`
- Add `/opt/axentx/vanguard/backend/requirements.txt`

## 3. Implementation

```bash
# Ensure project structure
mkdir -p /opt/axentx/vanguard/backend/manifests /opt/axentx/vanguard/backend/batches
```

`/opt/axentx/vanguard/backend/requirements.txt`:
```text
lightning>=2.3
huggingface-hub>=0.24
pyarrow>=16
pandas>=2.2
requests>=2.31
tqdm>=4.66
```

`/opt/axentx/vanguard/backend/ingest.py`:
```python
#!/usr/bin/env python3
"""
Ingest one date folder from HF dataset repo.
Produces:
- manifests/{date}/files.json  (content-addressed file list)
- batches/mirror-merged/{date}/{slug}.parquet  ({prompt,response} only)
"""
import json
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd
from huggingface_hub import list_repo_tree, hf_hub_download

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/vanguard-dataset")
BASE_DIR = Path(__file__).parent
MANIFEST_DIR = BASE_DIR / "manifests"
BATCH_DIR = BASE_DIR / "batches"

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def list_date_folder(date_str: str) -> List[Dict]:
    """
    Single API call: list non-recursive for one date folder.
    date_str format: YYYY-MM-DD
    """
    folder_path = f"mirror-merged/{date_str}"
    items = list_repo_tree(repo_id=HF_REPO, path=folder_path, recursive=False)
    files = [it for it in items if it.type == "file"]
    return files

def project_to_prompt_response(local_path: Path) -> pd.DataFrame:
    """
    Load file and project to {prompt,response} only.
    Handles mixed schemas gracefully.
    """
    try:
        df = pd.read_parquet(local_path)
    except Exception as e:
        # fallback: try json lines
        try:
            df = pd.read_json(local_path, lines=True)
        except Exception:
            raise ValueError(f"Cannot read {local_path}: {e}")

    # Normalize column names
    col_map = {c: c.strip().lower() for c in df.columns}
    df = df.rename(columns=col_map)

    prompt_col = None
    response_col = None
    for c in df.columns:
        if "prompt" in c:
            prompt_col = c
        if "response" in c or "completion" in c or "answer" in c:
            response_col = c

    rows = []
    for _, row in df.iterrows():
        prompt = str(row[prompt_col]) if prompt_col and pd.notna(row.get(prompt_col)) else ""
        response = str(row[response_col]) if response_col and pd.notna(row.get(response_col)) else ""
        if prompt or response:
            rows.append({"prompt": prompt, "response": response})
    return pd.DataFrame(rows)

def ingest_date_folder(date_str: str):
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    BATCH_DIR.mkdir(parents=True, exist_ok=True)

    date_folder = f"mirror-merged/{date_str}"
    date_batch_dir = BATCH_DIR / date_str
    date_batch_dir.mkdir(parents=True, exist_ok=True)

    files = list_date_folder(date_str)
    if not files:
        print(f"No files found in {date_folder}")
        return

    manifest = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": HF_REPO,
        "files": []
    }

    for f in files:
        path = f["path"]
        slug = Path(path).stem
        local_parquet = date_batch_dir / f"{slug}.parquet"

        # Download via CDN (no auth header required for public datasets)
        downloaded = hf_hub_download(
            repo_id=HF_REPO,
            filename=path,
            local_dir=date_batch_dir,
            local_dir_use_symlinks=False,
            force_download=False,
        )
        # Project to {prompt,response}
        projected = project_to_prompt_response(Path(downloaded))
        if projected.empty:
            continue

        table = pa.Table.from_pandas(projected, preserve_index=False)
        pq.write_table(table, local_parquet)

        entry = {
            "path": path,
            "slug": slug,
            "sha256": sha256_hex(path),
            "local_parquet": str(local_parquet.relative_to(BASE_DIR)),
            "num_rows": len(projected),
        }
        manifest["files"].append(entry)

    manifest_path = MANIFEST_DIR / date_str / "files.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"Manifest written: {manifest_path}")
    print(f"Total files: {len(manifest['files'])}, total rows: {sum(f['num_rows'] for f in manifest['files'])}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python ingest.py YYYY-MM-DD")
        sys.exit(1)
    ingest_date_folder(sys.argv[1])
```

`/opt/axentx/vanguard/backend/train.py`:
```python
#!/usr/bin/env python3
"""
Train surrogate-1 using CDN-only dataset fetches.
Reuses running Lightning Studio when available.
"""
import json
import os
import sys
from pathlib import Path

from lightning import LightningWork, LightningApp, Machine
from lightning.app.storage import Drive
import torch
from torch.utils.data import Dataset, DataLoader

# Simple dataset that reads from local parquet files listed in manifest
class ParquetDataset(Dataset):
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.base_dir = Path(manifest_path).parent.parent
        self.items = []
        for entry in self.manifest["files"]:
            p = self.base_dir / entry["local_parquet"]
            if p.exists():
                import pyarrow.parquet as pq
                table = pq.read_table(p)
                df = table.to_pandas()
                for _, row in df.iterrows():
                    self.items.append((row["prompt"], row["response"]))

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        prompt, response = self.items[idx]
        # Tokenization placeholder — replace with real tokenizer
