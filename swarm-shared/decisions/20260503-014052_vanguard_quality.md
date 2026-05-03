# vanguard / quality

## Final Synthesis — Best Parts + Correct, Actionable Resolution

I merged the strongest, non-redundant insights from both candidates and resolved contradictions in favor of **correctness** and **concrete actionability**.

---

## 1. Diagnosis (merged and tightened)

- Repeated authenticated `list_repo_tree` calls on every training/data-load cycle burn the HF API quota (1000/5min) and cause intermittent 429s.
- No persisted `(repo, dateFolder) → file-list` manifest exists, forcing re-enumeration and preventing CDN-only training workflows.
- Training/data-loading code likely uses `load_dataset(streaming=True)` or recursive listing on heterogeneous repos, risking `pyarrow.CastError` from mixed schemas.
- No guardrails to reuse running Lightning Studio instances; idle-stop kills training and wastes quota when recreating.
- Surrogate-1 ingestion probably writes enriched files with extra columns (`source`, `ts`) instead of strict `{prompt,response}` projection and attribution-by-filename.
- Local/mac orchestration may still attempt `model.from_pretrained()` or heavy compute instead of delegating to Lightning/Kaggle/Cerebras.

---

## 2. Proposed change (merged and prioritized)

Add a small, high-leverage utility layer to `/opt/axentx/vanguard` that enforces the CDN-bypass pattern and prevents the most common failure modes:

- **`vanguard/manifest.py`** (new) — single API call to list a date folder, persisted manifest with TTL, and CDN URL helpers.
- **`vanguard/data_manifest.py`** (alias/complement) — lightweight wrapper with TTL caching and robust CDN-only fetches.
- **`vanguard/train.py`** (patch) — use manifest + CDN-only fetches; project to `{prompt,response}` at parse time and validate schema homogeneity per file.
- **`vanguard/lightning_launcher.py`** (patch) — reuse running studios + idle handling; avoid recreating instances and wasting quota.

---

## 3. Implementation (merged, corrected, and actionable)

### Directory setup
```bash
cd /opt/axentx/vanguard
mkdir -p manifests .hf_manifests
```

### `manifest.py` (canonical, robust)
```python
#!/usr/bin/env python3
"""
Generate and load persisted file manifests for (repo, dateFolder).
Avoids repeated HF API list_repo_tree calls and enables CDN-only training.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from huggingface_hub import HfApi, list_repo_tree

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

api = HfApi()


def _manifest_path(repo_id: str, date_folder: str) -> Path:
    safe_repo = repo_id.replace("/", "_")
    return MANIFEST_DIR / safe_repo / f"{date_folder}.json"


def build_manifest(
    repo_id: str,
    date_folder: str,
    token: Optional[str] = None,
    ttl_seconds: int = 86400,
) -> List[str]:
    """
    Single API call to list top-level of date_folder and persist manifest.
    Returns sorted list of file paths (relative to repo root).
    """
    tree = list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        recursive=False,
        token=token,
    )
    files = sorted([f.rfilename for f in tree if f.type == "file"])

    out_path = _manifest_path(repo_id, date_folder)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "repo_id": repo_id,
                "date_folder": date_folder,
                "files": files,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": ttl_seconds,
            },
            indent=2,
        )
    )
    return files


def load_manifest(repo_id: str, date_folder: str) -> List[str]:
    p = _manifest_path(repo_id, date_folder)
    if not p.exists():
        raise FileNotFoundError(f"Manifest missing: {p}. Run build_manifest first.")
    data = json.loads(p.read_text())
    return data["files"]


def is_manifest_fresh(repo_id: str, date_folder: str) -> bool:
    p = _manifest_path(repo_id, date_folder)
    if not p.exists():
        return False
    data = json.loads(p.read_text())
    created = datetime.fromisoformat(data["created_utc"]).replace(tzinfo=timezone.utc)
    ttl = data.get("ttl_seconds", 86400)
    now = datetime.now(timezone.utc)
    return (now - created).total_seconds() < ttl


def cdn_url(repo_id: str, file_path: str) -> str:
    return f"https://huggingface.co/datasets/{repo_id}/resolve/main/{file_path}"
```

### `data_manifest.py` (lightweight wrapper with TTL and CDN fetch)
```python
# /opt/axentx/vanguard/data_manifest.py
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import requests

HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"


def list_date_folder_once(
    repo: str,
    date_folder: str,
    token: str,
    cache_dir: str = ".hf_manifests",
    ttl_seconds: int = 86400,
) -> List[str]:
    """
    Persist (repo, date_folder) -> file-list manifest to avoid repeated
    authenticated list_repo_tree calls. Uses TTL for freshness.
    """
    cache_path = Path(cache_dir) / repo.replace("/", "_") / f"{date_folder}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        created = datetime.fromisoformat(data["created_utc"]).replace(tzinfo=timezone.utc)
        if (now - created).total_seconds() < ttl_seconds:
            return data["files"]

    # Fetch fresh list
    resp = requests.get(
        f"{HF_API_BASE}/models/{repo}/tree",
        params={"path": date_folder, "recursive": "false"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json()
    files = sorted([f["path"] for f in items if f["type"] == "file"])

    cache_path.write_text(
        json.dumps(
            {
                "repo": repo,
                "date_folder": date_folder,
                "files": files,
                "created_utc": now.isoformat(),
                "ttl_seconds": ttl_seconds,
            },
            indent=2,
        )
    )
    return files


def fetch_via_cdn(repo: str, file_path: str) -> bytes:
    url = f"{HF_CDN_BASE}/{repo}/resolve/main/{file_path}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content
```

### `train.py` (patch — use manifest + CDN-only fetches)
```python
# Example patch for vanguard/train.py
import os
import pyarrow as pa
import pyarrow.parquet as pq
from vanguard.manifest import build_manifest, load_manifest, cdn_url, is_manifest_fresh
import requests

REPO = "myorg/surrogate-1"
DATE_FOLDER = "batches/mirror-merged/2026-04-29"
TOKEN = os.getenv("HF_TOKEN")  # only used during manifest build

# One-time or refresh if stale
if not is_manifest_fresh(REPO, DATE_FOLDER):
    files = build_manifest(REPO, DATE_FOLDER, token=TOKEN)
else:
    files = load_manifest(REPO, DATE_FOLDER)


def stream_rows():
    for f in files:
        url = cdn_url(REPO, f)
        resp = requests.get(url, timeout=30)
        resp.raise_for
