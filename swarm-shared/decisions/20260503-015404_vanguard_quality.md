# vanguard / quality

## 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` manifest: every training run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Training/data ingestion uses `load_dataset(streaming=True)` or repeated per-file API calls instead of CDN-only fetches, violating the CDN bypass pattern.
- No deterministic repo selection for commit-cap mitigation: writes concentrate on one repo and risk the 128/hr cap.
- No Lightning Studio reuse guard: scripts create new studios instead of reusing running ones, wasting ~80hr/mo quota.
- No idle-stop resilience: Lightning idle timeout kills training; no pre-run status check or auto-restart.

## 2. Proposed change

Add a small, high-leverage orchestration module that:
- Generates and caches a `batches/file-list-{repo}-{date}.json` after a single authenticated `list_repo_tree` call (Mac-side, run once per date folder).
- Embeds that list in training scripts so Lightning workers fetch via CDN URLs only (zero API calls during training).
- Deterministically maps `hash(slug) % 5` to sibling repos to spread writes and avoid commit caps.
- Reuses running LightStudios by name and auto-restarts stopped ones before `.run()`.

Scope: create `/opt/axentx/vanguard/orchestrate.py` and update training launcher to use it.

## 3. Implementation

```python
# /opt/axentx/vanguard/orchestrate.py
import json, hashlib, os, time
from pathlib import Path
from typing import List, Dict, Optional

try:
    from lightning import LightningWork, LightningApp, Machine
    from lightning.app import LightningFlow
    LAI_AVAILABLE = True
except Exception:
    LAI_AVAILABLE = False

HF_DATASETS_ROOT = "https://huggingface.co/datasets"
CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

def hf_cdn_url(repo: str, path: str) -> str:
    return f"{HF_DATASETS_ROOT}/{repo}/resolve/main/{path}"

def deterministic_repo(repo_base: str, siblings: int = 5) -> str:
    """Map repo+date to one of N sibling repos to avoid HF commit caps."""
    key = f"{repo_base}"
    idx = int(hashlib.sha256(key.encode()).hexdigest(), 16) % siblings
    return f"{repo_base}-s{idx}" if idx > 0 else repo_base

def build_file_list(
    repo: str,
    date_folder: str,
    client_fn,
    cache: bool = True
) -> List[str]:
    """
    Build and cache file-list for a date folder using a single list_repo_tree call.
    client_fn: must return an HF API client with list_repo_tree(repo, path, recursive=False).
    Returns CDN-capable paths (relative to repo root).
    """
    cache_path = CACHE_DIR / f"file-list-{repo}-{date_folder}.json"
    if cache and cache_path.exists():
        return json.loads(cache_path.read_text())

    client = client_fn()
    # Non-recursive per folder to minimize pagination/requests
    tree = client.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = []
    for item in tree:
        if item.rfilename.endswith((".parquet", ".jsonl", ".json")):
            files.append(item.rfilename)

    if cache:
        cache_path.write_text(json.dumps(files, indent=2))
    return files

def build_manifest(
    repo: str,
    date_folder: str,
    client_fn,
    cache: bool = True
) -> Dict:
    """Return manifest with CDN URLs and metadata for training."""
    files = build_file_list(repo, date_folder, client_fn, cache=cache)
    target_repo = deterministic_repo(repo)
    return {
        "repo": target_repo,
        "date_folder": date_folder,
        "files": files,
        "cdn_urls": [hf_cdn_url(target_repo, f) for f in files],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

# Lightweight Lightning Studio reuse helpers (optional, only if LAI available)
if LAI_AVAILABLE:
    from lightning.app import LightningFlow
    from lightning.app.utilities.cloud import _get_project
    from lightning.app.core.work import Work

    class StudioReuser(LightningFlow):
        def __init__(self, studio_name: str, machine: str = "lightningai/l40s-4x12gb"):
            super().__init__()
            self.studio_name = studio_name
            self.machine = machine
            self._studio = None

        def _find_running(self):
            from lightning.app import Teamspace
            for s in Teamspace.studios:
                if s.name == self.studio_name and s.status == "running":
                    return s
            return None

        def run(self, target: Work):
            studio = self._find_running()
            if studio is None:
                # create or start fresh
                from lightning.app import Studio
                studio = Studio(
                    name=self.studio_name,
                    machine=self.machine,
                    create_ok=True,
                )
            # Ensure running before submitting work
            if studio.status != "running":
                studio.start(machine=self.machine)
                # simple wait (in practice, poll status)
                time.sleep(30)
            studio.run(target)
            self._studio = studio
else:
    class StudioReuser:
        def __init__(self, *a, **kw): pass
        def run(self, target): raise RuntimeError("Lightning AI SDK not available")
```

Example usage in training launcher (`train.py` snippet):

```python
# /opt/axentx/vanguard/train.py  (excerpt)
import os
from orchestrate import build_manifest, hf_cdn_url

def get_hf_client():
    from huggingface_hub import HfApi
    return HfApi()

def main():
    repo = os.getenv("HF_DATASET_REPO", "myorg/surrogate-1")
    date_folder = os.getenv("DATE_FOLDER", "batches/mirror-merged/2026-04-29")

    manifest = build_manifest(repo, date_folder, get_hf_client, cache=True)

    # Lightning training will use CDN-only URLs (no HF API auth during data loading)
    urls = manifest["cdn_urls"]
    print(f"Prepared {len(urls)} files for CDN-only training.")

    # Pass urls to your dataloader (e.g., via webdataset or custom parquet loader)
    # Example: use webdataset with tar shards or direct parquet via pyarrow + fsspec
    # dataset = load_from_cdn_parquet(urls, columns=["prompt", "response"])
```

Cron/ops note (for ingestion):
- Run `python orchestrate.py --build-manifest ...` from Mac after rate-limit window clears (single API call), commit the `.cache/file-list-*.json` to repo or store in shared storage.
- Training pods reference the manifest and fetch via CDN only.

## 4. Verification

1. Generate manifest once:
   ```bash
   cd /opt/axentx/vanguard
   HF_DATASET_REPO=myorg/surrogate-1 DATE_FOLDER=batches/mirror-merged/2026-04-29 \
     python -c "from orchestrate import build_manifest; from huggingface_hub import HfApi; m=build_manifest('$HF_DATASET_REPO','$DATE_FOLDER',lambda:HfApi(),cache=True); print(m['repo'], len(m['cdn_urls']))"
   ```
   - Confirm: prints repo name and file count > 0.
   - Confirm: `.cache/file-list-*.json` exists and contains relative paths.

2. Validate CDN URLs bypass auth:
   ```bash
   curl -I $(python -c "from orchestrate import hf_cdn_url; print(hf_cdn_url('myorg/surrogate-1','batches/mirror-merged/2026-04-29/some.parquet'))")
   ```
   - Expect: `HTTP/2 200` (or 302/200) with no `WWW-Authenticate` challenge.

3. Lightning Studio reuse (if using LAI):
   ```python
   from orchestrate import StudioReuser
   from lightning import Work
   # create a dummy Work and run via StudioReuser;
