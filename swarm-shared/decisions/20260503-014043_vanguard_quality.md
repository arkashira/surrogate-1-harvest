# vanguard / quality

## Final synthesized answer (single, actionable)

### 1. Diagnosis (merged, prioritized)
- **Quota burn + 429s**: repeated authenticated `list_repo_tree` calls on every training/data-load exhaust the HF API quota (1000/5min).  
- **No manifest**: missing persisted `(repo, dateFolder) → file-list` forces re-enumeration and prevents reliable CDN-only training.  
- **Schema/cast risk**: training/data-loading code likely uses recursive listing or `load_dataset(streaming=True)` on heterogeneous repos, risking `pyarrow.CastError` on mixed-schema files.  
- **Studio churn**: no guard to reuse running Lightning Studio; idle-stop kills training and wastes quota via unnecessary recreation.  
- **Missing pre-flight checks**: no validation that required files exist locally/on CDN before attempting authenticated API calls.

### 2. Core change (high-leverage, minimal surface)
Add one utility module that:
- Persists a file-list manifest for `(repo, dateFolder)` after a single authenticated enumeration.
- Exposes a loader that training/data scripts import to get CDN-only paths (zero API calls during training).
- Reuses a running Lightning Studio by name instead of recreating.
- Adds schema-safe, CDN-only dataset loading (project only required columns; fail fast on schema mismatch).

Scope: create `/opt/axentx/vanguard/vanguard/manifest.py` and update `/opt/axentx/vanguard/train.py` (and provide `train_job.py` snippet).

### 3. Implementation

#### `/opt/axentx/vanguard/vanguard/manifest.py`
```python
# /opt/axentx/vanguard/vanguard/manifest.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"


def _api_get(url: str, token: Optional[str] = None, retries: int = 3) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(retries):
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 429:
            wait = 360
            print(f"[manifest] 429 rate-limited; waiting {wait}s")
            import time
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed to fetch {url} after {retries} retries")


def list_repo_folder(repo: str, folder: str, token: Optional[str] = None) -> List[str]:
    """
    Non-recursive folder listing. Returns file paths in folder.
    Uses authenticated API once; caller should cache result.
    """
    url = f"{HF_API_BASE}/datasets/{repo}/tree?path={folder}&recursive=false"
    tree = _api_get(url, token=token)
    return [item["path"] for item in tree if item["type"] == "file"]


def build_manifest(
    repo: str,
    date_folder: str,
    token: Optional[str] = None,
    out_dir: Optional[Path] = None,
    required_exts: Optional[List[str]] = None,
) -> Dict:
    """
    Build and persist manifest for (repo, date_folder).
    Manifest schema:
    {
      "repo": "...",
      "folder": "...",
      "created_utc": "...",
      "files": ["path1", "path2", ...],
      "cdn_prefix": "https://huggingface.co/datasets/repo/resolve/main/folder/"
    }
    """
    files = list_repo_folder(repo, date_folder, token=token)
    if required_exts:
        files = [f for f in files if any(f.endswith(ext) for ext in required_exts)]

    manifest = {
        "repo": repo,
        "folder": date_folder,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
        "cdn_prefix": f"{HF_CDN_BASE}/{repo}/resolve/main/{date_folder}/",
    }

    if out_dir is None:
        out_dir = Path.home() / ".cache" / "vanguard" / "manifests"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{repo.replace('/', '_')}__{date_folder.replace('/', '_')}.json"
    path = out_dir / slug
    path.write_text(json.dumps(manifest, indent=2))
    return manifest


def load_manifest(repo: str, date_folder: str) -> Optional[Dict]:
    slug = f"{repo.replace('/', '_')}__{date_folder.replace('/', '_')}.json"
    path = Path.home() / ".cache" / "vanguard" / "manifests" / slug
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def get_or_build_manifest(
    repo: str,
    date_folder: str,
    token: Optional[str] = None,
    required_exts: Optional[List[str]] = None,
) -> Dict:
    cached = load_manifest(repo, date_folder)
    if cached:
        return cached
    return build_manifest(repo, date_folder, token=token, required_exts=required_exts)


def cdn_urls_for_manifest(manifest: Dict) -> List[str]:
    prefix = manifest["cdn_prefix"]
    return [f"{prefix}{f}" for f in manifest["files"]]
```

#### `/opt/axentx/vanguard/train.py` (updated launcher)
```python
# /opt/axentx/vanguard/train.py
import os
from pathlib import Path

from lightning import Lightning, Machine, Teamspace

from vanguard.manifest import get_or_build_manifest, cdn_urls_for_manifest

HF_REPO = os.getenv("HF_REPO", "org/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", "batches/mirror-merged/2026-04-29")
HF_TOKEN = os.getenv("HF_TOKEN", None)
STUDIO_NAME = os.getenv("STUDIO_NAME", "surrogate-1-train")


def get_studio(name: str = STUDIO_NAME, machine: Machine = Machine.L40S):
    # Reuse running studio to save quota and avoid churn
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"[train] Reusing running studio: {s.name}")
            return s
    print(f"[train] Creating studio: {name}")
    return Teamspace.create_studio(
        name=name,
        machine=machine,
        create_ok=True,
    )


def run():
    # 1) Build/persist manifest once (single API call)
    manifest = get_or_build_manifest(
        HF_REPO,
        DATE_FOLDER,
        token=HF_TOKEN,
        required_exts=[".parquet", ".jsonl"],
    )
    urls = cdn_urls_for_manifest(manifest)
    print(f"[train] Found {len(urls)} files via CDN (zero API calls during training)")

    # 2) Reuse studio
    studio = get_studio(STUDIO_NAME)
    if studio.status != "Running":
        studio.start(machine=Machine.L40S)

    # 3) Launch training on studio using CDN-only URLs (no authenticated list during data load)
    manifest_path = (
        Path.home()
        / ".cache"
        / "vanguard"
        / "manifests"
        / f"{HF_REPO.replace('/', '_')}__{DATE_FOLDER.replace('/', '_')}.json"
    )
    job = studio.run(
        run_fn="train_job.py",
        requirements=["torch", "datasets", "pyarrow", "requests"],
        env={
            "MANIFEST_PATH": str(manifest_path),
            "HF_REPO": HF_REPO,
            "DATE_FOLDER": DATE_FOLDER,
        },
    )
    print(f"[train] Studio job submitted: {job}")


if __name__ == "__main__":
    run()
```

#### `train_job.py` (schema-safe, CDN-only loader snippet)
```python
# train_job.py
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet
