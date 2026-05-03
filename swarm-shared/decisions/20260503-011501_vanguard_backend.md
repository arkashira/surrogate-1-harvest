# vanguard / backend

## Final Consolidated Implementation

Below is the single, authoritative version that merges the strongest, non-contradictory parts of both proposals and resolves all conflicts in favor of **correctness + concrete actionability**.

### 1) Diagnosis (merged)
- Every backend request triggers authenticated `list_repo_tree`/`/api/` calls against HuggingFace, burning the 1000/5min quota and risking 429s.
- Dataset file downloads use authenticated API endpoints instead of public CDN URLs (`resolve/main/...`), adding auth overhead and quota exposure.
- No persisted `(repo, dateFolder)` manifest exists; ingestion and training re-enumerate files repeatedly instead of loading a once-generated file list.
- Training jobs embed no file list, so workers re-query HF; Lightning Studios may be re-created instead of reused, wasting quota and startup time.
- Backend invokes HF API synchronously in request context rather than delegating ingestion to an async/worker path, amplifying quota pressure during traffic spikes.

### 2) Proposed change (merged)
Create a lightweight backend service layer that:
- Generates and caches a `(repo, dateFolder)` manifest via a single authenticated `list_repo_tree` call (saved as JSON).
- Serves file lists from the manifest (no per-request HF API calls).
- Rewrites dataset download paths to use public CDN (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero auth.
- Adds utilities to reuse running Lightning Studios by name before creating new ones.
- Exposes a small CLI for manifest generation so orchestration jobs (not request handlers) populate caches.

Scope:
- Add `/opt/axentx/vanguard/backend/services/hf_manifest.py`
- Add `/opt/axentx/vanguard/backend/services/lightning_studio.py`
- Update data-loading/download helpers to prefer CDN URLs and accept an optional manifest path.

### 3) Implementation

```bash
# Ensure directories exist
mkdir -p /opt/axentx/vanguard/backend/services
```

#### `/opt/axentx/vanguard/backend/services/hf_manifest.py`

```python
"""
hf_manifest.py
Generate, cache, and serve (repo, dateFolder) file manifests for HF datasets.
Goals:
- Avoid per-request list_repo_tree calls.
- Use CDN for downloads (zero auth/rate-limit exposure).
- Provide CLI for pre-populating manifests in orchestration jobs.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download

CACHE_DIR = Path(os.getenv("HF_MANIFEST_CACHE_DIR", "/tmp/vanguard_hf_manifests"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

HF_API = HfApi()


def _cache_path(repo_id: str, date_folder: str) -> Path:
    safe = repo_id.replace("/", "_")
    return CACHE_DIR / f"{safe}__{date_folder}.json"


def generate_manifest(
    repo_id: str,
    date_folder: str,
    *,
    recursive: bool = False,
    overwrite: bool = False,
) -> Dict:
    """
    Generate manifest for repo_id/date_folder using a single list_repo_tree call.
    Returns:
      {
        "repo_id": "...",
        "date_folder": "...",
        "generated_at_utc": "...",
        "files": ["path1", "path2", ...]
      }
    """
    cache = _cache_path(repo_id, date_folder)
    if cache.exists() and not overwrite:
        return json.loads(cache.read_text())

    # Single API call: non-recursive list by folder; we expect date_folder to contain files.
    tree = HF_API.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=recursive)
    files = [
        item.rfilename
        for item in tree
        if not item.rfilename.endswith("/")  # files only
    ]

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
    }
    cache.write_text(json.dumps(manifest, indent=2))
    return manifest


def load_manifest(repo_id: str, date_folder: str) -> Optional[Dict]:
    cache = _cache_path(repo_id, date_folder)
    if cache.exists():
        return json.loads(cache.read_text())
    return None


def cdn_download_url(repo_id: str, file_path: str) -> str:
    """
    Public CDN URL that bypasses authenticated /api/ endpoints and rate limits.
    No Authorization header required.
    """
    # Normalize repo/file slashes and avoid double slashes in URL.
    repo_part = repo_id.strip("/")
    file_part = file_path.lstrip("/")
    return f"https://huggingface.co/datasets/{repo_part}/resolve/main/{file_part}"


def download_via_cdn(repo_id: str, file_path: str, local_path: str) -> str:
    """
    Download file using public CDN (no auth). Falls back to hf_hub_download if CDN fails.
    Returns local_path.
    """
    url = cdn_download_url(repo_id, file_path)
    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        p = Path(local_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return local_path
    except Exception:
        # fallback to authenticated download
        return hf_hub_download(
            repo_id=repo_id,
            filename=file_path,
            local_dir=os.path.dirname(local_path) if os.path.dirname(local_path) else None,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HF dataset manifest for caching.")
    parser.add_argument("repo_id", help="HF repo id (e.g. org/dataset)")
    parser.add_argument("date_folder", help="Folder path within repo (e.g. batches/mirror-merged/2026-04-29)")
    parser.add_argument("--recursive", action="store_true", help="List recursively")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite cached manifest")
    parser.add_argument("--output", help="Optional explicit output JSON path")
    args = parser.parse_args()

    try:
        manifest = generate_manifest(args.repo_id, args.date_folder, recursive=args.recursive, overwrite=args.overwrite)
    except Exception as exc:
        print(f"Failed to generate manifest: {exc}", file=sys.stderr)
        sys.exit(1)

    out = args.output or str(_cache_path(args.repo_id, args.date_folder))
    Path(out).write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
```

#### `/opt/axentx/vanguard/backend/services/lightning_studio.py`

```python
"""
lightning_studio.py
Reuse running Lightning Studios to save quota and avoid idle-stop restarts.
"""
from typing import Optional

from lightning import Studio, Teamspace


def get_or_create_studio(
    name: str,
    machine: str = "L40S",
    *,
    create_ok: bool = True,
    cloud: str = "lightning-public-prod",
) -> Studio:
    """
    Reuse a running studio if present; otherwise create one.
    Notes:
    - Lightning free tier -> public cloud (L40S max). H200 requires paid cloud.
    - If a studio exists but is stopped, attempt to start it before creating.
    """
    # Prefer running instance
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s

    if not create_ok:
        raise RuntimeError(f"Studio {name} not running and create_ok=False")

    # Try to start existing stopped studio
    for s in Teamspace.studios:
        if s.name == name:
            try:
                s.start(machine=machine)
                return s
            except Exception:
                # fallback to create
                break

    return Studio(
        name=name,
        machine=machine,
       
