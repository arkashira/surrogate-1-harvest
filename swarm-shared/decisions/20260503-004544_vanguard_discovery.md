# vanguard / discovery

## 1. Diagnosis

- No persisted `(repo, dateFolder)` file manifest exists → every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Recursive `list_repo_files` usage likely still present, causing pagination overhead and schema exposure to mixed-file repos.
- Training script probably uses `load_dataset(streaming=True)` or similar, which triggers per-batch API calls and schema-cast errors on heterogeneous repos.
- Lightning Studio lifecycle is likely recreated each run instead of reused, wasting 80hr/mo quota.
- No CDN-only path strategy embedded in training — authenticated API calls continue during data loading.

## 2. Proposed change

Create `/opt/axentx/vanguard/manifest.py` (single responsibility) and modify the training launcher to:
- Run `manifest.build(repo, date_folder)` once on the Mac (orchestration side) → produces `manifests/{repo}/{date_folder}.json`.
- Embed that manifest path in the Lightning training script so data loader uses only CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero authenticated API calls.
- Reuse a running Lightning Studio if present; otherwise start one (idempotent).

Scope:
- New file: `vanguard/manifest.py`
- Update: `vanguard/train_launcher.py` (or equivalent orchestration script) to call manifest and pass manifest path to training.
- Update: `vanguard/train.py` (or equivalent training script) to accept `--manifest` and use CDN-only downloads.

## 3. Implementation

### vanguard/manifest.py
```python
#!/usr/bin/env python3
"""
Build and cache a deterministic file manifest for a repo/date folder.
Goal: avoid authenticated HF API calls during training.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)


MANIFESTS_ROOT = Path(__file__).parent / "manifests"
MANIFESTS_ROOT.mkdir(exist_ok=True)


def build(repo: str, date_folder: str, out_dir: Path = MANIFESTS_ROOT) -> Path:
    """
    List top-level folder for repo/date_folder and save manifest.
    Uses non-recursive tree listing to minimize API calls.
    Returns path to manifest JSON.
    """
    # One API call: list_repo_tree(path=date_folder, recursive=False)
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [
        {
            "repo": repo,
            "path": item.rfilename,  # e.g. "2026-04-29/file.parquet"
            "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{item.rfilename}"
        }
        for item in items
        if not item.type == "directory"
    ]

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "note": "CDN-only manifest. Do not use authenticated HF API during training."
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    safe_repo = repo.replace("/", "_")
    out_path = out_dir / f"{safe_repo}__{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path


def load(manifest_path: Path) -> Dict:
    return json.loads(manifest_path.read_text())


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: manifest.py <repo> <date_folder>")
        sys.exit(1)
    repo, date_folder = sys.argv[1], sys.argv[2]
    p = build(repo, date_folder)
    print(f"Manifest written: {p}")
```

### vanguard/train_launcher.py (example orchestration update)
```python
#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

# Local imports
from manifest import build, load
from lightning import Teamspace, Studio, Machine  # pseudo import — adapt to actual SDK

REPO = "my-org/surrogate-1"
DATE_FOLDER = "2026-04-29"

def reuse_or_start_studio(name: str = "vanguard-train") -> Studio:
    for s in Teamspace.studios():
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print("Starting new studio...")
    return Studio.create(
        name=name,
        machine=Machine.L40S,
        cloud="lightning-public-prod"
    )

def main() -> None:
    # 1) Build manifest once (authenticated call allowed here)
    manifest_path = build(REPO, DATE_FOLDER)
    print(f"Manifest: {manifest_path}")

    # 2) Reuse or start Lightning Studio
    studio = reuse_or_start_studio("vanguard-train")

    # 3) Launch training with manifest (studio.run executes in cloud)
    script = Path(__file__).parent / "train.py"
    cmd = [
        sys.executable, str(script),
        "--manifest", str(manifest_path),
        "--repo", REPO,
        "--output", "s3://my-bucket/surrogate-1/run"
    ]
    # If studio.run supports local script upload, adapt accordingly.
    # Example fallback: package script+manifest and use studio.run(cmd)
    print("Launching training with manifest (CDN-only downloads)...")
    # studio.run(cmd)  # adapt to actual SDK
    # For local test:
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    main()
```

### vanguard/train.py (training script update — CDN-only loader)
```python
#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Iterator, Tuple

import torch
from torch.utils.data import IterableDataset, DataLoader
import requests


class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: Path):
        manifest = json.loads(manifest_path.read_text())
        self.urls = [f["cdn_url"] for f in manifest["files"] if f["cdn_url"].endswith(".parquet")]

    def __iter__(self) -> Iterator[Tuple[str, str]:
        for url in self.urls:
            # Stream parquet without auth; project to {prompt,response} here.
            # Lightweight: use pyarrow or fastparquet to read only needed cols.
            import pyarrow.parquet as pq
            import io
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            # Project to expected schema — adapt column names as needed.
            if "prompt" in table.column_names and "response" in table.column_names:
                for row in table.to_pylist():
                    yield row["prompt"], row["response"]
            else:
                # Fallback: try common aliases
                prompt_col = next((c for c in table.column_names if "prompt" in c.lower()), None)
                response_col = next((c for c in table.column_names if "response" in c.lower() or "completion" in c.lower()), None)
                if prompt_col and response_col:
                    for row in table.to_pylist():
                        yield row[prompt_col], row[response_col]
                else:
                    continue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    dataset = CDNParquetDataset(args.manifest)
    loader = DataLoader(dataset, batch_size=8, num_workers=2)

    # Minimal training loop placeholder
    for batch in loader:
        prompts, responses = batch
        # Tokenize and train step here
        print(f"Batch size: {len(prompts)}")
        break

    print("Training step completed (CDN-only).")


if __name__ == "__main__":
    main()
```

## 4. Verification

1. Run manifest build (Mac
