# vanguard / discovery

# Final Synthesis (Best Parts + Correctness + Actionability)

## Diagnosis (merged)
- **No persisted `(repo, dateFolder)` file manifest** → every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- **Data loader likely uses recursive enumeration or `load_dataset(streaming=True)` on heterogeneous repos** → triggers PyArrow schema errors and amplifies rate-limit exposure.
- **Training script probably runs data enumeration on the Lightning Studio node** (wastes quota and compute) instead of Mac orchestrator with CDN-only fetches.
- **No reuse guard for Lightning Studio** → idle stop/start cycles kill training and waste 80hr/mo quota.
- **Manifest and file list are ephemeral** → no deterministic shard selection for surrogate-1 ingestion (hash-slug → sibling repo) and no CDN bypass strategy embedded.

## Proposed Change (merged)
Create **one** canonical module:  
`/opt/axentx/vanguard/discovery/manifest.py` (~120–150 lines) that:

1. On the Mac (or any orchestrator) lists **one** `dateFolder` via a **single authenticated `list_repo_tree(recursive=False)`** call.
2. Persists a **deterministic JSON manifest**:
   - `{repo, dateFolder, created_at, files: [{path, size, sha?, cdn_url}]}`
3. Embeds the file list into training scripts so Lightning training does **CDN-only fetches**  
   (`https://huggingface.co/datasets/.../resolve/main/...`) with **zero authenticated API calls** during data load.
4. Adds **lightweight Studio reuse + status helpers** to avoid recreation and idle-timeout deaths.
5. Adds **deterministic sibling repo selection** (hash-slug → sibling repo) for commit-cap scaling.

## Implementation (merged + hardened)

```python
#!/usr/bin/env python3
"""
Generate and reuse a deterministic file manifest for (repo, dateFolder)
so training can use CDN-only fetches and avoid HF API rate limits.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from huggingface_hub import HfApi, list_repo_tree
except ImportError:
    HfApi = None  # type: ignore
    list_repo_tree = None  # type: ignore

# ---- constants ----
MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)
CDN_BASE = "https://huggingface.co/datasets"
HF_API_RETRY = 360  # seconds after 429

# ---- helpers ----
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _slug_to_repos(slug: str, n_siblings: int = 5) -> List[str]:
    """Deterministic sibling repo selection for commit-cap scaling."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n_siblings
    if idx == 0:
        return [slug]
    return [f"{slug}-s{idx}"]

def _ensure_hf_api(token: Optional[str] = None):
    if HfApi is None or list_repo_tree is None:
        raise RuntimeError("huggingface_hub not installed. pip install huggingface_hub")
    return HfApi(token=token)

# ---- manifest ----
def build_manifest(
    repo: str,
    date_folder: str,
    token: Optional[str] = None,
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Single authenticated list_repo_tree call (non-recursive) for one dateFolder.
    Returns and persists manifest.
    """
    api = _ensure_hf_api(token)
    prefix = f"{date_folder}/"
    retries = 3
    for attempt in range(1, retries + 1):
        try:
            tree = list_repo_tree(repo=repo, path=prefix, recursive=False, token=token)
            break
        except Exception as e:
            if "429" in str(e):
                if attempt == retries:
                    raise
                print(f"HF 429, sleeping {HF_API_RETRY}s (attempt {attempt})", file=sys.stderr)
                time.sleep(HF_API_RETRY)
                continue
            raise

    files: List[Dict[str, Any]] = []
    for entry in tree:
        # entry.path is like "2026-04-29/file.parquet"
        if entry.path is None or not entry.path.startswith(prefix):
            continue
        rel = entry.path[len(prefix):] if entry.path.startswith(prefix) else entry.path
        if not rel or rel.endswith("/"):
            continue
        cdn_url = f"{CDN_BASE}/{repo}/resolve/main/{entry.path}"
        files.append(
            {
                "path": entry.path,
                "rel_path": rel,
                "size": getattr(entry, "size", None),
                "lfs": getattr(entry, "lfs", None),
                "cdn_url": cdn_url,
            }
        )

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": _now_iso(),
        "files": sorted(files, key=lambda x: x["rel_path"]),
        "total_files": len(files),
        "strategy": "cdn-only",
        "note": "Use cdn_url for training; zero authenticated API calls during data load.",
    }

    out = (out_dir or MANIFEST_DIR) / f"{repo.replace('/', '_')}_{date_folder}.json"
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out} ({len(files)} files)", file=sys.stderr)
    return manifest

def load_manifest(repo: str, date_folder: str, manifest_dir: Optional[Path] = None) -> Dict[str, Any]:
    manifest_dir = manifest_dir or MANIFEST_DIR
    p = manifest_dir / f"{repo.replace('/', '_')}_{date_folder}.json"
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {p}. Run build_manifest first.")
    return json.loads(p.read_text())

# ---- lightning studio helpers ----
def reuse_or_create_studio(
    name: str,
    target_machine: str = "L40S",
    *,
    create_ok: bool = True,
    max_idle_minutes: int = 10,
):
    """
    Reuse a running studio to save quota. If stopped, restart it.
    Requires lightning-sdk installed in the environment that calls this.
    """
    try:
        from lightning.fabric.utilities.exceptions import LightningFabricException
        from lightning.pytorch.studio import Studio
        from lightning.pytorch.studio.studio import Machine
    except ImportError as e:
        raise RuntimeError("lightning not available. Install lightning to use studio helpers.") from e

    # Map friendly names to Machine enum values if possible; fallback to string
    try:
        machine = Machine[target_machine] if hasattr(Machine, target_machine) else target_machine
    except Exception:
        machine = target_machine

    for s in Studio.list():
        if s.name == name:
            if s.status == "running":
                print(f"Reusing running studio: {name}", file=sys.stderr)
                return s
            if s.status in ("stopped", "idle"):
                print(f"Restarting stopped studio: {name}", file=sys.stderr)
                s.start(machine=machine)
                return s
            # other states (starting, etc.) — return as-is
            print(f"Studio {name} in state {s.status}; returning existing.", file=sys.stderr)
            return s

    if not create_ok:
        raise RuntimeError(f"Studio {name} not found and create_ok=False")
    print(f"Creating studio: {name} on {machine}", file=sys.stderr)
    return Studio.create(name=name, machine=machine, create_ok=True)

# ---- CLI ----
def _cli_build():
    parser = argparse.ArgumentParser(description="Build HF dataset manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., 'owner/dataset')")
    parser.add_argument("--date-folder", required=True, help="Date folder (e.g., '2026-04-29')
