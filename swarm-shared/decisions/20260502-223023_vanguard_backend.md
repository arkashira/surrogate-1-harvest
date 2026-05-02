# vanguard / backend

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged the strongest, non-contradictory parts from both proposals and fixed correctness/actionability issues (imports, retry/backoff behavior, CLI defaults, and Lightning Studio reuse).

### 1. Diagnosis (merged, corrected)
- No canonical discovery entrypoint (`/vanguard/backend/__init__.py` or `main.py`) → ad-hoc execution and violates `#knowledge-rag #graph #hub` pattern.
- Missing HF CDN-bypass file-list generation for surrogate-1 training → future training jobs will hit 429 rate limits during data loading.
- No Lightning Studio reuse guard → each training run risks quota waste and idle-stop kills.
- No centralized HF ingestion config (repo, date folder, CDN root) → brittle hardcoded paths.
- No safe HF tree listing with rate-limit backoff → ingestion scripts will fail unpredictably.

### 2. Proposed change (merged, actionable)
Create:
- `/opt/axentx/vanguard/backend/ingest/__init__.py`
- `/opt/axentx/vanguard/backend/ingest/cdn_file_list.py`
- `/opt/axentx/vanguard/backend/main.py`

Behavior:
- List HF dataset tree (non-recursive, one folder) once and write `file_list.json`.
- Embed CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) for zero-API training fetches.
- Detect and reuse a running Lightning Studio by name to avoid quota churn.
- Use robust HF API calls with 429 backoff; avoid recursive `list_repo_files`.

### 3. Implementation (corrected + ready to run)

```bash
# Ensure structure
mkdir -p /opt/axentx/vanguard/backend/ingest
touch /opt/axentx/vanguard/backend/ingest/__init__.py
```

`/opt/axentx/vanguard/backend/ingest/cdn_file_list.py`
```python
#!/usr/bin/env python3
"""
Generate CDN-only file list for HF dataset folder to avoid API rate limits during training.
Usage:
  python cdn_file_list.py --repo <datasets/repo> --path <subfolder> --out file_list.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from huggingface_hub import HfApi  # type: ignore

CDN_ROOT = "https://huggingface.co/datasets"
HF_API_RETRY = 5
HF_API_INITIAL_BACKOFF = 10  # seconds
HF_API_MAX_BACKOFF = 360     # seconds after repeated 429

def _hf_api() -> HfApi:
    # token optional for public repos; omit to reduce auth surface
    return HfApi()

def list_folder_tree(repo: str, path: str = "") -> List[Dict[str, Any]]:
    """
    List single-level tree for a dataset repo folder (non-recursive).
    Avoids list_repo_files recursive pagination and heavy API usage.
    """
    api = _hf_api()
    for attempt in range(HF_API_RETRY):
        try:
            # recursive=False ensures one page (or few) and avoids 100x pagination
            tree = api.list_repo_tree(repo=repo, path=path, recursive=False)
            if isinstance(tree, dict) and "entries" in tree:
                return tree["entries"]
            if isinstance(tree, list):
                return tree
            return []
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429:
                wait = min(HF_API_INITIAL_BACKOFF * (2**attempt), HF_API_MAX_BACKOFF)
                print(f"HF API 429, retry {attempt+1}/{HF_API_RETRY} in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Exhausted retries listing HF repo tree: {repo}@{path}")

def build_cdn_file_list(repo: str, folder: str = "") -> List[Dict[str, Any]]:
    entries = list_folder_tree(repo, folder)
    out: List[Dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("type") != "file":
            continue
        rpath = e.get("path") or e.get("name")
        if not rpath:
            continue
        cdn_url = f"{CDN_ROOT}/{repo}/resolve/main/{rpath}"
        out.append(
            {
                "repo": repo,
                "path": rpath,
                "cdn_url": cdn_url,
                "size": e.get("size"),
                "lfs": bool(e.get("lfs", {}).get("oid")) if isinstance(e.get("lfs"), dict) else False,
            }
        )
    return out

def write_file_list(repo: str, folder: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    listing = build_cdn_file_list(repo, folder)
    out_path.write_text(json.dumps(listing, indent=2))
    return out_path

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN file list for HF dataset folder")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/repo_name)")
    parser.add_argument("--path", default="", help="Subfolder within dataset repo")
    parser.add_argument("--out", default="file_list.json", help="Output JSON path")
    args = parser.parse_args()

    out = write_file_list(args.repo, args.path, Path(args.out))
    print(f"Wrote {len(json.loads(out.read_text()))} files to {out}")

if __name__ == "__main__":
    main()
```

`/opt/axentx/vanguard/backend/main.py`
```python
#!/usr/bin/env python3
"""
Vanguard backend entrypoint.
- Generates CDN file lists for HF datasets (rate-limit-safe).
- Reuses running Lightning Studio to preserve quota.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from lightning import Studio, Teamspace

    LIGHTNING_AVAILABLE = True
except Exception:
    Studio = None
    Teamspace = None
    LIGHTNING_AVAILABLE = False

from vanguard.backend.ingest.cdn_file_list import build_cdn_file_list

LIGHTNING_CLOUD_PRIORITY = ["lightning-lambda-prod", "lightning-public-prod"]

def reuse_running_studio(name: str) -> Studio | None:
    if not LIGHTNING_AVAILABLE:
        print("Lightning SDK unavailable; skipping studio reuse")
        return None
    try:
        studios = Teamspace.studios()
        for s in studios:
            if getattr(s, "name", None) == name and getattr(s, "status", None) == "Running":
                print(f"Reusing running studio: {name}")
                return s
    except Exception as exc:
        print(f"Studio reuse check failed: {exc}")
    return None

def start_or_reuse_studio(name: str, machine: str = "L40S") -> Studio | None:
    existing = reuse_running_studio(name)
    if existing:
        return existing

    if not LIGHTNING_AVAILABLE:
        print("Lightning SDK not available; cannot start studio")
        return None

    for cloud in LIGHTNING_CLOUD_PRIORITY:
        try:
            print(f"Attempting studio on cloud={cloud} machine={machine}")
            st = Studio.create(
                name=name,
                machine=machine,
                cloud=cloud,
                create_ok=True,
            )
            print(f"Created studio {name} on {cloud}")
            return st
        except Exception as exc:
            print(f"Cloud {cloud} failed: {exc}")
            continue
    print("No cloud available for studio")
    return None

def cmd_cdn_list(args: argparse.Namespace) -> int:
    repo = args.repo
    folder = args.folder or ""
    out = Path(args.out)
    listing = build_cdn_file_list(repo, folder)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(listing, indent=2))
    print(f"CDN list written to {out} ({len(listing)} files)")
    return 0

def cmd_studio(args: argparse.Namespace) -> int:
    st = start_or_re
