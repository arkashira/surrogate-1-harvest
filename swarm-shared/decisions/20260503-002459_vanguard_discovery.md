# vanguard / discovery

## 1. Diagnosis

- No persisted `(repo, dateFolder)` manifest exists → every training run re-enumerates via authenticated HF API → quota burn + 429 risk.
- Training likely uses recursive `list_repo_tree` or `load_dataset(streaming=True)` during data loading, causing repeated API calls and schema-cast errors on mixed-file repos.
- No CDN-only fetch path in training pipeline → authenticated API traffic remains the primary data source, violating HF CDN bypass guidance.
- Missing deterministic repo selection for commit-cap mitigation (no sibling repo hashing) when writing enriched parquet artifacts.
- No Lightning Studio reuse guard before `.run()` → idle stop kills training and burns quota via repeated studio creation.

## 2. Proposed change

Create `/opt/axentx/vanguard/pipelines/discovery/manifest.py` and update `/opt/axentx/vanguard/pipelines/discovery/train.py` (or create it) to:

- Add `build_manifest(repo, date_folder, out_path)` that calls `list_repo_tree(path=date_folder, recursive=False)` once, saves `{repo}/{path}` list to JSON.
- Add `load_manifest(path)` used by training to construct CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero API calls during data load.
- Add `pick_sibling_repo(slug, n=5)` to spread enriched writes across sibling repos deterministically.
- Add `get_or_create_studio(name, machine)` that reuses running studios and restarts idle ones.

Scope: two files (manifest.py + train.py), ~120 lines total.

## 3. Implementation

```bash
# Ensure directory exists
mkdir -p /opt/axentx/vanguard/pipelines/discovery
```

### `/opt/axentx/vanguard/pipelines/discovery/manifest.py`

```python
#!/usr/bin/env python3
"""
Manifest utilities for vanguard discovery pipeline.
- Build a (repo, date_folder) file manifest via single HF API call.
- Provide CDN-only URLs for training.
- Deterministic sibling repo selection to bypass HF commit cap.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi, list_repo_tree

HF_API = HfApi()


def build_manifest(
    repo: str,
    date_folder: str,
    out_path: Path | str,
    revision: str = "main",
) -> Dict:
    """
    Single authenticated API call to list files in date_folder (non-recursive).
    Saves manifest:
      {
        "repo": "...",
        "revision": "...",
        "date_folder": "...",
        "files": ["f1.parquet", "f2.parquet", ...],
        "cdn_base": "https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}"
      }
    """
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False, revision=revision)
    files = sorted([entry.path.split("/")[-1] for entry in tree if entry.type == "file"])

    manifest = {
        "repo": repo,
        "revision": revision,
        "date_folder": date_folder.rstrip("/"),
        "files": files,
        "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder.rstrip('/')}",
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def load_manifest(path: Path | str) -> Dict:
    return json.loads(Path(path).read_text())


def cdn_urls(manifest: Dict) -> List[str]:
    base = manifest["cdn_base"].rstrip("/")
    return [f"{base}/{f}" for f in manifest["files"]]


def pick_sibling_repo(slug: str, n: int = 5, prefix: str = "vanguard") -> str:
    """
    Deterministic sibling repo selection to spread writes across repos
    and bypass HF commit cap (128/hr/repo).

    Returns repo name like: vanguard-0 or vanguard-1
    """
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest, 16) % n
    return f"{prefix}-{idx}"


def get_or_create_studio(name: str, machine: str = "L40S"):
    """
    Reuse running Lightning Studio; if stopped, restart it.
    Avoids quota burn from repeated studio creation.
    """
    from lightning_sdk import Teamspace, Studio, Machine

    teamspace = Teamspace()
    for studio in teamspace.studios:
        if studio.name == name:
            if studio.status == "running":
                return studio
            # restart idle/stopped studio
            studio.start(machine=Machine(machine))
            return studio

    # create if not exists
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )
```

### `/opt/axentx/vanguard/pipelines/discovery/train.py`

```python
#!/usr/bin/env python3
"""
Vanguard discovery training entrypoint (Lightning Studio).
Uses CDN-only fetches via pre-built manifest to avoid HF API rate limits.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from torch.utils.data import Dataset, DataLoader

from manifest import build_manifest, load_manifest, cdn_urls, get_or_create_studio


class CDNParquetDataset(Dataset):
    """
    Lightweight dataset that streams parquet files via CDN URLs.
    Projects to {prompt, response} only at parse time.
    """

    def __init__(self, manifest_path: Path | str):
        manifest = load_manifest(manifest_path)
        self.urls = cdn_urls(manifest)
        # Keep minimal metadata; avoid adding source/ts cols per pattern.
        self.batches_dir = Path("batches/mirror-merged")

    def __len__(self) -> int:
        return len(self.urls)

    def __getitem__(self, idx: int):
        import pyarrow.parquet as pq
        import requests

        url = self.urls[idx]
        # CDN fetch (no auth header) — bypasses API rate limits.
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        # Load from bytes and project to {prompt, response}
        table = pq.read_table(pq.ParquetFile(pq.ParquetFile(pq.BufferReader(resp.content))))
        # Adjust column names if needed; keep projection minimal.
        prompt = table["prompt"][0].as_py() if "prompt" in table.column_names else ""
        response = table["response"][0].as_py() if "response" in table.column_names else ""
        return {"prompt": prompt, "response": response}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/vanguard-dataset")
    parser.add_argument("--date-folder", default="2026-04-29")
    parser.add_argument("--manifest", default="manifest.json")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--rebuild-manifest", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if args.rebuild_manifest or not manifest_path.exists():
        print("Building manifest (single API call)...")
        build_manifest(args.repo, args.date_folder, manifest_path)

    print("Using CDN-only dataset...")
    dataset = CDNParquetDataset(manifest_path)
    loader = DataLoader(dataset, batch_size=8, shuffle=True, num_workers=0)

    # Minimal training loop placeholder (replace with real surrogate-1 training).
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    for batch in loader:
        # Example: tokenize and forward pass here.
        _ = batch["prompt"]
        _ = batch["response"]
        break

    # Example studio reuse pattern (if running under Lightning).
    try:
        studio = get_or_create_studio(name="vanguard-discovery-l40s", machine="L40S")
        print(f"Studio status: {studio.status}")
    except Exception:
        # Lightning SDK may not be available locally; continue without.
        pass

   
