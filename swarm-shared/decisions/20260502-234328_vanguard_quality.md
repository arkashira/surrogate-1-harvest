# vanguard / quality

## Final Consolidated Solution

### 1. Diagnosis (merged)
- **Rate-limit exposure**: every training run re-calls `list_repo_tree`/`list_repo_files`; HF API 429 (1000 req/5 min) is inevitable at scale.
- **Studio lifecycle misuse**: recreation burns quota; idle-stop destroys state and forces full restarts instead of resuming.
- **Schema violations**: ingestion injects extra metadata (`source`, `ts`) and non-deterministic paths, breaking surrogate-1 schema and parquet bloat.
- **No CDN bypass**: training scripts perform authenticated HF API fetches instead of raw CDN downloads, adding latency and quota pressure.
- **Missing readiness checks**: `.run()` on Lightning Studio proceeds without verifying studio health, causing silent failures.
- **Deterministic write target missing**: all ingestion targets a single repo, risking HF commit cap and non-reproducible runs.

---

### 2. Proposed Change (unified)
Create `/opt/axentx/vanguard/ops/prepare_manifest.py` (single-purpose, <150 LOC) that:
- Accepts `repo` + `date_folder` and produces deterministic `manifests/{repo_safe}/{date}_files.json` containing only `{"path": "..."}`.
- Uses `list_repo_tree(path, recursive=False)` per subfolder to minimize calls and stay under pagination/rate limits.
- Exposes `get_file_list(repo, date, force=False)` for training scripts to import; no HF API calls during training.

Update `/opt/axentx/vanguard/train/run_surrogate.py` (or equivalent launcher) to:
- Import and use the persisted manifest.
- Download exclusively via CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with `requests` (no auth).
- Reuse a running Lightning Studio (name derived from `surrogate-{repo_safe}-{date_folder}`) or start if stopped; never recreate.
- Add lightweight readiness check before `.run()` (studio status + manifest existence).
- Enforce surrogate-1 schema in the loader: project raw files to `{prompt,response}` only; drop all injected metadata.
- Use deterministic repo selection for writes (configurable target repo or local cache) to avoid HF commit cap.

---

### 3. Implementation

```bash
# /opt/axentx/vanguard/ops/prepare_manifest.py
#!/usr/bin/env bash
# Lightweight orchestrator: calls the python helper with args
# Usage: ./prepare_manifest.py <repo> <date_folder> [--force]
set -euo pipefail
export SHELL=/bin/bash
exec python3 "$(dirname "$0")/prepare_manifest.py" "$@"
```

```python
# /opt/axentx/vanguard/ops/prepare_manifest.py
#!/usr/bin/env python3
"""
Generate and cache a file manifest for a HF dataset repo/date folder.
Keeps HF API usage minimal (one tree call per immediate subfolder).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Dict, Any

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

API = HfApi()
MANIFEST_ROOT = Path(__file__).resolve().parents[2] / "manifests"
MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)

def _safe_name(repo: str) -> str:
    return repo.replace("/", "_").replace("\\", "_")

def list_subtree_safe(repo: str, path: str, max_attempts: int = 5) -> List[Dict[str, Any]]:
    """List immediate children non-recursively; tolerate 429 by waiting."""
    for attempt in range(max_attempts):
        try:
            items = API.list_repo_tree(repo=repo, path=path, recursive=False)
            out = []
            for it in items:
                if isinstance(it, dict):
                    out.append(it)
                else:
                    out.append({
                        "path": getattr(it, "path", str(it)),
                        "size": getattr(it, "size", None),
                        "type": getattr(it, "type", "file")
                    })
            return out
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                wait = 60 * (2 ** attempt)
                print(f"Rate limited on {repo}:{path}. Waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Exhausted retries for list_repo_tree on {repo}:{path}")

def build_manifest(repo: str, date_folder: str, force: bool = False) -> Path:
    """
    repo: e.g. 'datasets/opus-mt'
    date_folder: subfolder under repo root, e.g. '2026-04-29'
    """
    safe_repo = _safe_name(repo)
    manifest_path = MANIFEST_ROOT / safe_repo / f"{date_folder}_files.json"
    if manifest_path.exists() and not force:
        print(f"Manifest exists: {manifest_path}", file=sys.stderr)
        return manifest_path

    entries = []
    top = list_subtree_safe(repo, date_folder)
    for item in top:
        p = item.get("path") or item.get("name") or str(item)
        kind = item.get("type", "file")
        if kind == "directory":
            children = list_subtree_safe(repo, p)
            for c in children:
                cp = c.get("path") or c.get("name") or str(c)
                entries.append({"path": cp})
        else:
            entries.append({"path": p})

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    print(f"Wrote {len(entries)} entries to {manifest_path}", file=sys.stderr)
    return manifest_path

def get_file_list(repo: str, date_folder: str, force: bool = False) -> List[str]:
    mp = build_manifest(repo, date_folder, force=force)
    with open(mp, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [item["path"] for item in data if item.get("path")]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create HF file manifest for CDN-based training.")
    parser.add_argument("repo", help="HF dataset repo, e.g. datasets/opus-mt")
    parser.add_argument("date_folder", help="Subfolder in repo, e.g. 2026-04-29")
    parser.add_argument("--force", action="store_true", help="Regenerate even if exists")
    args = parser.parse_args()
    mp = build_manifest(args.repo, args.date_folder, force=args.force)
    print(str(mp))
```

```python
# /opt/axentx/vanguard/train/run_surrogate.py  (minimal diff example)
# Add near top:
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

from prepare_manifest import get_file_list
import requests

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_fetch(path: str, repo: str, timeout: int = 30) -> bytes:
    url = f"{CDN_ROOT}/{repo}/resolve/main/{path}"
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

def project_to_surrogate(raw) -> dict:
    """
    Enforce surrogate-1 schema: {prompt, response}.
    Drop injected metadata (source, ts) and any non-schema fields.
    Implement per-format parsing as needed.
    """
    # Example stub: adapt to actual file format (jsonl, parquet, csv, etc.)
    # Return exactly {"prompt": "...", "response": "..."}
    raise NotImplementedError("Implement projection for your data format")

# In training loader:
#   file_paths = get_file_list("datasets/opus-mt", "2026-04-29")
#   for p in file_paths:
#       data = cdn_fetch(p, "datasets/opus-mt")
#       record = project_to
