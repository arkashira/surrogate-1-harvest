# surrogate-1 / quality

## Final Implementation Plan (≤2 h)

**Highest-value improvement**: Replace fragile shell-only ingestion with a **manifest-driven, CDN-bypass pipeline** that:
- eliminates Hugging Face API rate limits during training data loads,
- prevents mixed-schema `pyarrow` `CastError`s,
- deterministically assigns shards, and
- keeps one simple upload step via `huggingface_hub`.

---

### Concrete changes (prioritized)

1. Add `bin/build_manifest.py` (run once on Mac / CI before training)
   - Uses `huggingface_hub` **only here** to list each date folder once and write `public-dataset-root/{date}/manifest.json`.
   - Manifest contains relative file paths and optional metadata (size, sha256) for integrity.

2. Add `bin/worker.py` (CDN-only worker)
   - Reads `{date}/manifest.json` (never lists via API again).
   - Downloads via **CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header → bypasses HF API rate limits entirely.
   - Projects heterogeneous files to `{prompt, response}` **only at parse time** → avoids mixed-schema `CastError`.
   - Deterministic `hash(slug) % 16` → `SHARD_ID`.
   - Writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.parquet` containing **only** `prompt` and `response` (no `source`, no `ts`).
   - Uploads the single parquet file via `huggingface_hub` (uses `HF_TOKEN` only here).

3. Update `bin/dataset-enrich.sh`
   - Thin wrapper that sets `SHELL=/bin/bash`, `set -euo pipefail`.
   - Exports `SHARD_ID` from matrix and invokes `python3 bin/worker.py --shard $SHARD_ID --date $DATE`.

4. Update `.github/workflows/ingest.yml`
   - Explicitly use `bash`.
   - Matrix `shard: [0..15]`.
   - Pass `DATE` (default today) and `MANIFEST_PATH` (optional override).
   - `HF_TOKEN` used only in worker upload step; CDN downloads are anonymous.

5. Add/confirm dependencies
   - `requirements.txt` (or `requirements-dev.txt`): `huggingface_hub`, `pyarrow`, `pandas`, `requests`, `tqdm`.

---

### Code snippets (merged + hardened)

#### `bin/build_manifest.py`
```python
#!/usr/bin/env python3
"""
One-time manifest builder for a date folder.
Usage:
  python3 bin/build_manifest.py --date 2026-05-03 --out manifests/2026-05-03/manifest.json
"""

import argparse
import json
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-03")
    parser.add_argument("--out", required=True, help="Output manifest path")
    args = parser.parse_args()

    api = HfApi()
    items = api.list_repo_tree(
        repo_id=HF_REPO,
        path=args.date,
        recursive=False,
        repo_type="dataset",
    )
    files = [it.rfilename for it in items if it.type == "file"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(files, f, indent=2)

    print(f"Wrote {len(files)} entries -> {out_path}")

if __name__ == "__main__":
    main()
```

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass worker for surrogate-1 public-dataset ingestion.

Usage:
  python3 bin/worker.py --shard 0 --date 2026-05-03 [--manifest manifests/2026-05-03/manifest.json]
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi
from tqdm import tqdm

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/{HF_REPO}/resolve/main"

def shard_for_slug(slug: str, n_shards: int = 16) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % n_shards

def load_manifest(manifest_path: Path) -> list[str]:
    with open(manifest_path) as f:
        data = json.load(f)
    if isinstance(data, list) and all(isinstance(x, str) for x in data):
        return data
    raise ValueError(f"Invalid manifest format: {manifest_path}")

def parse_to_pair(local_path: Path) -> dict:
    suffix = local_path.suffix.lower()
    try:
        if suffix == ".parquet":
            df = pq.read_table(local_path).to_pandas()
        elif suffix == ".jsonl":
            df = pd.read_json(local_path, lines=True)
        elif suffix == ".json":
            df = pd.read_json(local_path)
        else:
            with open(local_path) as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if len(lines) >= 2:
                return {"prompt": lines[0], "response": " ".join(lines[1:])}
            elif len(lines) == 1:
                return {"prompt": lines[0], "response": ""}
            else:
                return {"prompt": "", "response": ""}

        prompt_col = next((c for c in df.columns if "prompt" in c.lower()), None)
        response_col = next((c for c in df.columns if "response" in c.lower() or "completion" in c.lower()), None)

        if prompt_col and response_col:
            row = df.iloc[0]
            return {"prompt": str(row[prompt_col]), "response": str(row[response_col])}

        if len(df.columns) >= 2:
            return {"prompt": str(df.iloc[0, 0]), "response": str(df.iloc[0, 1])}
        elif len(df.columns) == 1:
            return {"prompt": str(df.iloc[0, 0]), "response": ""}
        else:
            return {"prompt": "", "response": ""}
    except Exception:
        return {"prompt": "", "response": ""}

def download_via_cdn(rel_path: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"{CDN_ROOT}/{rel_path}"
    out_path = out_dir / Path(rel_path).name
    if out_path.exists():
        return out_path

    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return out_path

def run_worker(shard_id: int, date: str, manifest_path: Path, out_root: Path = Path("output")) -> None:
    print(f"Worker shard={shard_id} date={date} manifest={manifest_path}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    paths = load_manifest(manifest_path)
    my_paths = [p for p in paths if shard_for_slug(p, 16) == shard_id]
    print(f"Processing {len(my_paths)} files for shard {shard_id}")

    work_dir = out_root / "tmp" / f"shard{shard_id}"
    rows = []

    for rel_path in tqdm(my_paths, desc=f"Shard {shard_id}"):
        try:
            local = download_via_cdn(rel_path, work_dir)
            pair = parse_to_pair(local)
            if pair["prompt"].strip()
