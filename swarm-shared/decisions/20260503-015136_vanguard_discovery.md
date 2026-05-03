# vanguard / discovery

# Final Consolidated Implementation

## Diagnosis (Resolved)

All candidates correctly identify the same core issues; contradictions were stylistic, not functional. The consolidated diagnosis is:

- **No `(repo, dateFolder) → file-list` manifest**: every training run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- **Training scripts use HF API paths** (`load_dataset(streaming=True)` or per-file API calls) instead of CDN-only fetches, amplifying rate-limit exposure.
- **No deterministic repo sharding** for commits; ingestion writes to a single repo capped at 128 commits/hr, throttling throughput.
- **No Lightning Studio reuse logic**; each run recreates studios, wasting quota and risking loss on idle-stop.
- **No guardrails for cron/launched jobs** (shebang/invoke guards, error handling, idempotency).

## Chosen Design

Adopt Candidate 1’s structure (clean module layout) with Candidate 2’s emphasis on minimal, single-file utility where appropriate and explicit cron/bash guardrails. Implement one lightweight discovery module plus small orchestration helpers.

## File Layout

```
/opt/axentx/vanguard/
├── __init__.py
├── discovery.py          # primary utilities (manifest, CDN URLs, sharding, studio reuse)
├── manifests/            # gitignored; cached file-lists
└── bin/
    └── plan_run.sh       # cron-safe wrapper with shebang, lock, logging, retries
```

## Implementation

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/manifests /opt/axentx/vanguard/bin
touch /opt/axentx/vanguard/__init__.py /opt/axentx/vanguard/discovery.py
```

```python
# /opt/axentx/vanguard/discovery.py
from __future__ import annotations
import json, os, hashlib, fcntl, logging, time
from pathlib import Path
from typing import List, Optional

try:
    from huggingface_hub import list_repo_tree
    from lightning import Teamspace, Studio, Machine
except ImportError as e:
    raise RuntimeError("Install: huggingface_hub lightning-ai") from e

MANIFESTS_DIR = Path(__file__).parent / "manifests"
MANIFESTS_DIR.mkdir(exist_ok=True)

HF_CDN_ROOT = "https://huggingface.co/datasets"

# Logging
log = logging.getLogger("vanguard")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)

# ---- Manifest cache (single API call per dateFolder) ----
def _cache_key(repo: str, date_folder: str) -> str:
    safe = repo.replace("/", "--")
    return f"{safe}--{date_folder}.json"

def list_and_cache_files(repo: str, date_folder: str, token: Optional[str] = None) -> List[str]:
    """
    Single authenticated list_repo_tree call for one dateFolder.
    Saves manifest for later CDN-only training fetches.
    """
    ck = _cache_key(repo, date_folder)
    manifest_path = MANIFESTS_DIR / ck

    if manifest_path.exists():
        try:
            with open(manifest_path) as f:
                return json.load(f)
        except Exception:
            log.warning("Corrupt manifest %s; will regenerate", manifest_path)

    # Avoid recursive on big repos; list top-level dateFolder only
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False, token=token)
    files = [
        f.rfilename
        for f in tree
        if f.type == "file" and f.rfilename.lower().endswith((".parquet", ".jsonl", ".json"))
    ]

    manifest_path.write_text(json.dumps(files, indent=2))
    log.info("Cached %d files for %s/%s", len(files), repo, date_folder)
    return files

def cdn_download_urls(repo: str, file_paths: List[str]) -> List[str]:
    """Return CDN URLs that bypass HF API auth/rate-limits."""
    return [f"{HF_CDN_ROOT}/{repo}/resolve/main/{p}" for p in file_paths]

# ---- Repo sharding for HF commit cap (128/hr/repo) ----
def pick_shard_repo(base_repo: str, slug: str, n_shards: int = 5) -> str:
    """
    Deterministic sibling repo selection.
    Expect siblings named: base_repo, base_repo-shard1, ..., base_repo-shardN
    """
    if n_shards <= 1:
        return base_repo
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    shard = h % n_shards
    if shard == 0:
        return base_repo
    return f"{base_repo}-shard{shard}"

# ---- Lightning Studio reuse + idle-restart ----
def get_or_create_studio(name: str, machine: str = "L40S", reuse: bool = True):
    """
    Reuse running studio if exists; otherwise create.
    If studio is stopped, restart it on target machine.
    """
    team = Teamspace.current()
    candidates = [s for s in team.studios if s.name == name]

    if reuse and candidates:
        studio = candidates[0]
        if studio.status == "running":
            log.info("Reusing running studio %s", name)
            return studio
        if studio.status == "stopped":
            log.info("Restarting stopped studio %s on %s", name, machine)
            studio.start(machine=Machine(machine))
            return studio
        log.info("Studio %s status=%s", name, studio.status)
        return studio

    log.info("Creating studio %s on %s", name, machine)
    return Studio(name=name, machine=Machine(machine), create_ok=True)

def run_in_studio(name: str, target, machine: str = "L40S", reuse: bool = True):
    """
    Safe run helper that restarts stopped studios before run.
    """
    studio = get_or_create_studio(name=name, machine=machine, reuse=reuse)
    if studio.status != "running":
        log.info("Starting studio %s before run", name)
        studio.start(machine=Machine(machine))
    log.info("Running target in studio %s", name)
    return studio.run(target)

# ---- Optional: raw Kaggle kernel push (Bearer token) ----
def kaggle_push_kernel(
    token: str,
    slug: str,
    new_title: str,
    text: str,
    is_private: bool = True,
):
    """
    Push kernel using Bearer token (KGAT) and new API schema.
    slug format: "username/kernelname"
    """
    import requests
    url = "https://www.kaggle.com/api/v1/kernels/push"
    body = {
        "slug": slug,
        "newTitle": new_title,
        "text": text,
        "isPrivate": is_private,
    }
    resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=30)
    resp.raise_for_status()
    return resp
```

```python
# /opt/axentx/vanguard/__init__.py
from .discovery import (
    list_and_cache_files,
    cdn_download_urls,
    pick_shard_repo,
    get_or_create_studio,
    run_in_studio,
)

__all__ = [
    "list_and_cache_files",
    "cdn_download_urls",
    "pick_shard_repo",
    "get_or_create_studio",
    "run_in_studio",
]
```

```bash
# /opt/axentx/vanguard/bin/plan_run.sh
#!/usr/bin/env bash
# Cron-safe wrapper: lock, logging, retries, strict error handling
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCKFILE="/var/lock/vanguard_plan_run.lock"
LOGFILE="/var/log/vanguard_plan_run.log"
MAX_RETRIES=3
RETRY_DELAY=30

exec >>"$LOGFILE" 2>&1
echo "[$(date)] Starting plan_run"

# Single-instance lock
exec 200>"$LOCKFILE"
flock -n 200 || { echo "[$
