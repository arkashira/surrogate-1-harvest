# vanguard / backend

## Final Consolidated Implementation

**Core diagnosis (unified):**  
- Repeated `list_repo_tree`/`load_dataset` calls trigger HF API 429 (1000 req/5 min).  
- Lightning Studio recreation burns quota; idle-stop kills training and forces full restart.  
- Dynamic per-run data path resolution prevents CDN-only fetches and causes re-authentication each epoch.  
- No deterministic repo selection for HF commit-cap (128/hr/repo) and no guardrails against local model loading on dev machines.  
- Mixed-schema heterogeneous repos cause pyarrow `CastError` when using `load_dataset(streaming=True)`.

**Scope:** ~200 lines across three focused modules + launcher + training stub.

---

### 1) Create directory structure
```bash
mkdir -p /opt/axentx/vanguard/src/vanguard/{cache,orchestration,train}
mkdir -p /opt/axentx/vanguard/scripts
```

---

### 2) `/opt/axentx/vanguard/src/vanguard/cache/file_list_cache.py`
```python
# /opt/axentx/vanguard/src/vanguard/cache/file_list_cache.py
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx

CACHE_DIR = Path(__file__).parent.parent.parent.parent / ".cache" / "file_lists"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

HF_CDN_BASE = "https://huggingface.co/datasets"
HF_API_BASE = "https://huggingface.co/api"


def _cache_path(repo: str, folder: str) -> Path:
    safe = repo.replace("/", "_")
    return CACHE_DIR / f"{safe}__{folder.strip('/').replace('/', '_') or 'root'}.json"


def list_repo_tree_cached(
    repo: str,
    folder: str = "",
    ttl_minutes: int = 60,
    hf_token: Optional[str] = None,
) -> List[str]:
    """
    Return relative file paths under folder.
    Uses cache to avoid HF API 429. Bypasses auth for CDN downloads.
    """
    cp = _cache_path(repo, folder)
    now = datetime.now(timezone.utc)

    if cp.exists():
        data = json.loads(cp.read_text())
        cached_at = datetime.fromisoformat(data["_cached_at"])
        if now - cached_at < timedelta(minutes=ttl_minutes):
            return [p for p in data["paths"] if p]

    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    url = f"{HF_API_BASE}/datasets/{repo}/tree"
    params = {"path": folder, "recursive": "false"}
    resp = httpx.get(url, headers=headers, params=params, timeout=30.0)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("retry-after", 360))
        raise RuntimeError(f"HF 429: retry after {retry_after}s")
    resp.raise_for_status()
    items = resp.json()

    paths: List[str] = []
    for item in items:
        if item.get("type") == "file":
            paths.append(item["path"])

    payload = {
        "_cached_at": now.isoformat(),
        "repo": repo,
        "folder": folder,
        "paths": paths,
    }
    cp.write_text(json.dumps(payload, indent=2))
    return paths


def resolve_cdn_urls(repo: str, file_paths: List[str]) -> List[str]:
    """
    Convert dataset file paths to CDN URLs (no auth, bypasses API rate limits).
    """
    return [
        f"{HF_CDN_BASE}/{repo}/resolve/main/{p}"
        for p in file_paths
    ]


def pick_shard_repo(base_repo: str, slug: str, n_shards: int = 5) -> str:
    """
    Deterministic repo selection to spread HF commit load across siblings.
    Example: base_repo='org/mirror' -> 'org/mirror-shard-2'
    """
    if n_shards < 2:
        return base_repo
    shard_id = hash(slug) % n_shards
    return f"{base_repo}-shard-{shard_id}"
```

---

### 3) `/opt/axentx/vanguard/src/vanguard/orchestration/studio_manager.py`
```python
# /opt/axentx/vanguard/src/vanguard/orchestration/studio_manager.py
from __future__ import annotations

import os
import sys
from typing import Optional

try:
    from lightning import Studio, Teamspace
except Exception:  # graceful fallback when lightning not installed
    Studio = None
    Teamspace = None


def _guard_mac_local() -> None:
    """
    Prevent accidental local model loading on dev machines.
    """
    if sys.platform == "darwin":
        raise RuntimeError(
            "Mac local orchestration blocked. "
            "Run this only in Lightning Cloud or CI with HF credentials."
        )


def reuse_or_create_studio(
    name: str,
    machine: str = "L40S",
    cloud: str = "lightning-public-prod",
    idle_timeout_minutes: int = 30,
) -> Optional[Studio]:
    """
    Reuse running studio if exists; otherwise create.
    Avoids recreating and burning Lightning quota.
    Enforces idle-stop guard before .run().
    """
    _guard_mac_local()

    if Studio is None or Teamspace is None:
        return None

    for s in Teamspace.studios:
        if s.name == name:
            if s.status == "running":
                return s
            if s.status == "stopped":
                try:
                    s.start(machine=machine, cloud=cloud)
                    return s
                except Exception:
                    break

    return Studio(
        name=name,
        machine=machine,
        cloud=cloud,
        create_ok=True,
        idle_timeout=idle_timeout_minutes,
    )
```

---

### 4) `/opt/axentx/vanguard/scripts/train_launcher.py`
```python
#!/usr/bin/env python3
# /opt/axentx/vanguard/scripts/train_launcher.py
# Mac-only orchestration: do NOT run model.from_pretrained() here.
# This script prepares file-list and launches Lightning Studio training.

import json
import sys
from pathlib import Path

REPO = "datasets/example-corpus"
FOLDER = "batches/mirror-merged/2026-05-02"
CACHE_JSON = Path("./file_list.json")

# Ensure running in environment with vanguard installed
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.vanguard.cache.file_list_cache import list_repo_tree_cached, resolve_cdn_urls
from src.vanguard.orchestration.studio_manager import reuse_or_create_studio


def main() -> None:
    paths = list_repo_tree_cached(REPO, FOLDER, ttl_minutes=60)
    urls = resolve_cdn_urls(REPO, paths)
    CACHE_JSON.write_text(json.dumps({"urls": urls, "paths": paths}, indent=2))
    print(f"Cached {len(urls)} files -> {CACHE_JSON}")

    studio = reuse_or_create_studio(name="vanguard-surrogate-train", machine="L40S")
    if studio:
        studio.run(
            run_name="train-run-cdn",
            entry_script="src/vanguard/train/train.py",
            arguments=[
                "--file_list", str(CACHE_JSON),
                "--epochs", "1",
                "--batch_size", "8",
            ],
            dependencies=["requirements.txt"],
        )
        print("Studio run submitted (reuse/cached file-list enabled).")
    else:
        print("Lightning not available; skipping studio launch.")


if __name__ == "__main__":
    main()
```

---

### 5) `/opt/axentx/vanguard/src/vanguard/train/train.py`
```python
# /opt/axentx/vanguard/src/vanguard/train/train.py
# Expects --file_list pointing to JSON with "urls" list.
# Uses CDN URLs only (no HF API calls during training).

from __future__ import annotations

import argparse
import json
from
