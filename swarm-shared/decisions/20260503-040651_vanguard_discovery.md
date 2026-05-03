# vanguard / discovery

## 1. Diagnosis
- No content-addressed manifest for dataset snapshots → training scripts re-scan HF repos at runtime and trigger 429 rate-limits + non-reproducible epochs.
- `enriched/` contains mixed-schema parquet (extra `source`, `ts` cols) → `pyarrow.CastError` in surrogate-1 training.
- No pre-computed file list for a date folder → each run re-enumerates repo tree and risks API limits.
- No local cache or CDN-only fetch path in training code → data loader still uses `load_dataset`/HF API during training.
- No deterministic mapping from manifest to Lightning training input → manual path stitching invites drift.

## 2. Proposed change
Add a single, content-addressed manifest + a small loader that uses CDN-only fetches during training:
- Create `vanguard/data/manifest.py` — exports `build_manifest(date: str) -> dict` and `load_manifest(date: str) -> list[dict]` (each item: `repo`, `path`, `sha256`, `size`, `url`).
- Create `vanguard/data/loader.py` — `iter_cdn_parquet(manifest_items, columns=("prompt","response"))` that streams via `pd.read_parquet(url)` from CDN URLs (no HF API).
- Update surrogate-1 training script to accept `--date` and `--manifest` and use `iter_cdn_parquet` instead of `load_dataset`.

## 3. Implementation

```bash
# /opt/axentx/vanguard
mkdir -p data
```

```python
# data/manifest.py
from __future__ import annotations
import json, hashlib, os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

HF_DATASETS_ROOT = "https://huggingface.co/datasets"
# Example repo; replace with real one(s) used by surrogate-1
REPO = "surrogate-1/dataset-mirror"

def _cdn_url(repo: str, path: str) -> str:
    return f"{HF_DATASETS_ROOT}/{repo}/resolve/main/{path}"

def _hash_slug(repo: str, path: str) -> str:
    return hashlib.sha256(f"{repo}::{path}".encode()).hexdigest()[:16]

def build_manifest(date: str, repo: str = REPO, folder: str = "batches/mirror-merged") -> List[Dict[str, Any]]:
    """
    Build manifest for a date folder.
    NOTE: list_repo_tree should be called once (from Mac) and saved as JSON.
    This function assumes `file_list.json` exists locally for the date.
    """
    file_list_path = Path("lists") / date / "file_list.json"
    if not file_list_path.exists():
        raise FileNotFoundError(
            f"Pre-saved file list not found: {file_list_path}. "
            "Run once on Mac: client.list_repo_tree(repo, path=folder/{date}, recursive=True) -> save JSON."
        )

    with open(file_list_path, "r", encoding="utf-8") as f:
        entries = json.load(f)  # expect [{"path": "...", "type": "file", "size": ...}, ...]

    items = []
    for e in entries:
        if e.get("type") != "file":
            continue
        p = e["path"]
        if not p.lower().endswith(".parquet"):
            continue
        url = _cdn_url(repo, p)
        items.append({
            "repo": repo,
            "path": p,
            "sha256": _hash_slug(repo, p),
            "size": e.get("size", 0),
            "url": url,
            "date": date,
        })
    return items

def save_manifest(date: str, items: List[Dict[str, Any]], out_dir: str = "manifests") -> str:
    out_path = Path(out_dir) / f"{date}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    return str(out_path)

def load_manifest(date: str, out_dir: str = "manifests") -> List[Dict[str, Any]]:
    p = Path(out_dir) / f"{date}.json"
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)
```

```python
# data/loader.py
from __future__ import annotations
import pandas as pd
from typing import Iterator, Tuple, Optional
from .manifest import load_manifest

def iter_cdn_parquet(
    items: list[dict],
    columns: Optional[Tuple[str, ...]] = ("prompt", "response"),
    chunksize: int = 10_000,
) -> Iterator[pd.DataFrame]:
    """
    Yield DataFrames from CDN parquet URLs without HF API calls.
    Keeps memory bounded via chunksize (if supported by remote filesystem).
    """
    for item in items:
        url = item["url"]
        try:
            # Use pyarrow filesystem for remote parquet; falls back to pandas if needed
            df = pd.read_parquet(url, columns=columns)
        except Exception as exc:
            # Log and skip corrupt/unavailable files to avoid crashing training
            print(f"Skipping {url}: {exc}")
            continue

        if df.empty:
            continue

        # Ensure expected columns exist
        missing = set(columns or []) - set(df.columns)
        if missing:
            print(f"Missing columns {missing} in {url}; skipping")
            continue

        yield df
```

```python
# train_surrogate1.py  (minimal diff example)
# Add near top:
import argparse
from vanguard.data.manifest import load_manifest
from vanguard.data.loader import iter_cdn_parquet

# Replace dataset loading section:
# OLD:
#   ds = load_dataset("surrogate-1/dataset-mirror", streaming=True, split="train")
#   ...
# NEW:
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--manifest", default="manifests", help="Manifest directory")
    args = parser.parse_args()

    items = load_manifest(args.date, out_dir=args.manifest)
    # Build iterable of rows
    def row_iter():
        for df in iter_cdn_parquet(items, columns=("prompt", "response")):
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"]}

    # Use row_iter() with your dataloader / trainer
    # Example: trainer.fit(model, train_dataloaders=DataLoader(MyIterableDataset(row_iter()), ...))
```

```bash
# Make helper script to pre-save file list (run once per date from Mac)
# scripts/save_file_list.py
import json
from pathlib import Path
# Use huggingface_hub only on Mac (or wherever rate-limit window is open)
from huggingface_hub import list_repo_tree

REPO = "surrogate-1/dataset-mirror"
FOLDER = "batches/mirror-merged"

def main(date: str):
    out = Path("lists") / date / "file_list.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # recursive=True required to get files; run sparingly.
    tree = list_repo_tree(REPO, path=f"{FOLDER}/{date}", recursive=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(list(tree), f, indent=2)
    print(f"Saved {len(tree)} entries to {out}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python save_file_list.py YYYY-MM-DD")
        sys.exit(1)
    main(sys.argv[1])
```

## 4. Verification
1. On Mac (or where HF token is available), run once:
   ```bash
   python scripts/save_file_list.py 2026-04-29
   ```
   Confirm `lists/2026-04-29/file_list.json` exists and contains parquet entries.

2. Build and save manifest:
   ```
