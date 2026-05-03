# vanguard / backend

## Final Consolidated Solution

### 1. Diagnosis (merged)
- **No persisted manifest** for `(repo, dateFolder)` → every training/inference run triggers authenticated `list_repo_tree` → burns HF API quota (1000/5min) and risks 429s.
- **Authenticated `/api/` downloads** instead of public CDN → unnecessary auth overhead and tighter rate limits during training/inference.
- **No idempotent manifest write / resume** → ingestion duplicates work and can’t resume reliably.
- **No fallback when HF API is rate-limited** (no CDN-only mode) → training/inference stalls.
- **No deterministic repo selection for writes** (single repo ingestion) → will hit HF commit cap (128/hr) when scaled.
- **Missing cache invalidation / staleness handling** → stale manifests cause missing-file training errors.
- **No separation between orchestration and training data paths** → Mac orchestrator still performs heavy HF API work instead of passing a static file list to Lightning.

### 2. Scope & Files
- **Create**: `/opt/axentx/vanguard/backend/services/hf_ingest.py`  
  - Manifest build + CDN download + deterministic write-repo selector + staleness-aware reload.
- **Create/update**: `/opt/axentx/vanguard/backend/config.py`  
  - Repo siblings, manifest dir, staleness TTL, retry/backoff settings.
- **Create**: `/opt/axentx/vanguard/backend/data/manifest.py`  
  - Lightweight loader/writer used by orchestrator and training (shared contract).
- **Modify**: `/opt/axentx/vanguard/backend/data/dataloader.py`  
  - Accept prebuilt manifest or file list; use CDN-only downloads; add retry/backoff and fallback.

### 3. Implementation

```bash
mkdir -p /opt/axentx/vanguard/backend/services
mkdir -p /opt/axentx/vanguard/backend/data
```

```python
# /opt/axentx/vanguard/backend/config.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent.parent

# Manifest storage
VANGUARD_MANIFEST_DIR = os.getenv("VANGUARD_MANIFEST_DIR", str(BASE_DIR / "data/manifests"))

# HF settings
HF_REPO_SIBLINGS = os.getenv(
    "HF_REPO_SIBLINGS",
    "axentx/vanguard-data,axentx/vanguard-data-1,axentx/vanguard-data-2,axentx/vanguard-data-3,axentx/vanguard-data-4"
).split(",")

# Staleness and retry
MANIFEST_STALENESS_SECONDS = int(os.getenv("MANIFEST_STALENESS_SECONDS", "3600"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF_FACTOR = float(os.getenv("RETRY_BACKOFF_FACTOR", "0.5"))
CDN_TIMEOUT = int(os.getenv("CDN_TIMEOUT", "60"))
```

```python
# /opt/axentx/vanguard/backend/services/hf_ingest.py
import json
import hashlib
import os
import time
from pathlib import Path
from typing import List, Optional

import requests
from hugginggingface import HfApi

from vanguard.backend.config import (
    HF_REPO_SIBLINGS,
    VANGUARD_MANIFEST_DIR,
    MANIFEST_STALENESS_SECONDS,
    MAX_RETRIES,
    RETRY_BACKOFF_FACTOR,
    CDN_TIMEOUT,
)

HF_API = HfApi()
MANIFEST_DIR = Path(VANGUARD_MANIFEST_DIR)
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)


def _manifest_path(repo: str, date_folder: str) -> Path:
    safe_repo = repo.replace("/", "_")
    return MANIFEST_DIR / f"{safe_repo}__{date_folder}.json"


def _is_stale(manifest_file: Path) -> bool:
    if not manifest_file.exists():
        return True
    age = time.time() - manifest_file.stat().st_mtime
    return age > MANIFEST_STALENESS_SECONDS


def build_manifest(repo: str, date_folder: str, force: bool = False) -> Path:
    """
    Build or reuse a manifest for repo/date_folder.
    Uses a single non-recursive list_repo_tree call per folder to minimize API usage.
    Manifest contains only file paths (no content).
    """
    manifest_file = _manifest_path(repo, date_folder)
    if manifest_file.exists() and not force and not _is_stale(manifest_file):
        return manifest_file

    # Single API call: non-recursive per folder (avoids heavy pagination)
    items = HF_API.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    file_paths = [it.rfilename for it in items if it.type == "file"]

    manifest_file.write_text(
        json.dumps({"repo": repo, "date_folder": date_folder, "files": file_paths}, indent=2)
    )
    return manifest_file


def download_file_cdn(
    repo: str,
    file_path: str,
    dest: Path,
    timeout: int = CDN_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    backoff_factor: float = RETRY_BACKOFF_FACTOR,
) -> Path:
    """
    Download via public CDN (no Authorization header) to bypass /api/ rate limits.
    URL: https://huggingface.co/datasets/{repo}/resolve/main/{file_path}
    Includes retry/backoff for transient failures.
    """
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return dest
        except Exception as exc:  # noqa: BLE001 - retry on network/HTTP issues
            last_exception = exc
            if attempt < max_retries:
                sleep_time = backoff_factor * (2 ** attempt)
                time.sleep(sleep_time)
            else:
                break

    raise RuntimeError(f"Failed to download {url} after {max_retries + 1} attempts") from last_exception


def pick_write_repo(slug: str) -> str:
    """
    Deterministic repo selection for writes to spread HF commit load.
    Uses hash(slug) % N to pick sibling repo.
    """
    if not HF_REPO_SIBLINGS:
        raise ValueError("HF_REPO_SIBLINGS not configured")
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(HF_REPO_SIBLINGS)
    return HF_REPO_SIBLINGS[idx].strip()


def load_manifest(repo: str, date_folder: str, force_refresh: bool = False) -> List[str]:
    manifest_file = _manifest_path(repo, date_folder)
    if force_refresh or not manifest_file.exists() or _is_stale(manifest_file):
        build_manifest(repo, date_folder, force=force_refresh)
    data = json.loads(manifest_file.read_text())
    return data["files"]
```

```python
# /opt/axentx/vanguard/backend/data/manifest.py
import json
from pathlib import Path
from typing import List, Optional

from vanguard.backend.services.hf_ingest import build_manifest, load_manifest


def get_or_create_manifest(repo: str, date_folder: str, force: bool = False) -> List[str]:
    """
    Orchestrator-facing helper: returns file list for repo/date_folder.
    Builds manifest if missing/stale/forced.
    """
    if force:
        build_manifest(repo, date_folder, force=True)
    return load_manifest(repo, date_folder, force_refresh=force)


def write_manifest(repo: str, date_folder: str, files: List[str]) -> Path:
    """
    Write a manifest manually (useful for Mac orchestrator to embed static file lists
    for Lightning training jobs).
    """
    from vanguard.backend.services.hf_ingest import _manifest_path

    manifest_file = _manifest_path(repo, date_folder)
    manifest_file
