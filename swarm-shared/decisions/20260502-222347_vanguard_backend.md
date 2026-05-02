# vanguard / backend

## 1. Diagnosis
- No canonical discovery entrypoint for backend workflows → violates `#knowledge-rag #graph #hub` and forces ad-hoc exploration.
- No persisted HF CDN-bypass file-list strategy → surrogate-1 training will hit API rate limits when listing big repos.
- No Lightning Studio reuse guard → training iterations risk quota waste and idle-stop deaths.
- Orchestrator exists but lacks safe, idempotent bootstrapping for date-scoped file lists and studio lifecycle.
- Missing small, high-leverage backend utility to unify hub insight + file-list + studio reuse for downstream tasks.

## 2. Proposed change
Add `/opt/axentx/vanguard/backend/discovery.py` (single module) and update `/opt/axentx/vanguard/backend/orchestrator.py` to invoke it safely via CLI flag `--discovery`. Scope:
- Expose `build_discovery_report(date_str: str) -> dict` that:
  - Queries top hub (e.g., "MOC") via knowledge-rag patterns.
  - Uses HF API once (rate-limit safe) to `list_repo_tree` for the date folder and persists `file_list.json` for CDN-bypass.
  - Checks running Lightning studios and returns reusable candidates.
- Keep orchestrator changes minimal: add argparse branch and call `build_discovery_report`, write outputs under `discovery/YYYY-MM-DD/`.

## 3. Implementation

### File: `/opt/axentx/vanguard/backend/discovery.py`
```python
#!/usr/bin/env python3
"""
Discovery utilities for vanguard backend.
- Top hub insight (knowledge-rag pattern)
- HF CDN-bypass file list for a date folder
- Lightning Studio reuse check
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None  # optional for HF CDN URLs

# Optional Lightning imports (fail gracefully if not installed)
try:
    from lightning.app import Studio, Teamspace
    from lightning.app.utilities.cloud import get_cloud
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False
    Teamspace = Studio = type("Stub", (), {})  # type: ignore

HF_REPO_DATASETS = "datasets"  # configurable if needed
HF_PROJECT = os.getenv("HF_PROJECT", "your-org/your-dataset-repo")
DATE_FMT = "%Y-%m-%d"


def top_hub_insight() -> Dict[str, Any]:
    """
    Knowledge-rag pattern: review most-connected hub before planning.
    This is a lightweight placeholder that can be replaced with
    an actual RAG query against your graph. For now, returns a
    deterministic stub and attempts to invoke project-local helpers.
    """
    # Try local CLI helper if present (e.g., scripts/knowledge-rag)
    candidates = [
        Path("scripts/knowledge-rag"),
        Path("tools/knowledge-rag"),
        Path("knowledge-rag"),
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            try:
                out = subprocess.check_output([str(p), "top-hub"], stderr=subprocess.DEVNULL, text=True, timeout=15)
                return {"hub": out.strip() or "MOC", "source": p.name, "method": "local-cli"}
            except Exception:
                pass

    # Fallback deterministic insight
    return {"hub": "MOC", "source": "fallback", "method": "default"}


def list_hf_date_folder(date_str: str, repo: str, folder_prefix: str = "batches/mirror-merged") -> List[str]:
    """
    HF API strategy:
    - Single list_repo_tree call (non-recursive) for the date folder.
    - Avoid recursive list_repo_files on big repos.
    - Save list for CDN-bypass training (zero API calls during load).
    """
    try:
        from huggingface_hub import HfApi
    except ImportError as e:
        raise RuntimeError("huggingface_hub required for HF listing") from e

    api = HfApi()
    folder_path = f"{folder_prefix}/{date_str}"
    # recursive=False keeps pagination small; we only need direct children
    items = api.list_repo_tree(repo=repo, path=folder_path, repo_type=HF_REPO_DATASETS, recursive=False)
    # items may be tree entries; normalize paths
    files = []
    for it in items:
        if hasattr(it, "path"):
            files.append(it.path)
        elif isinstance(it, str):
            files.append(it)
        else:
            files.append(str(it))
    return sorted(files)


def cdn_urls_for_files(file_paths: List[str], repo: str) -> List[str]:
    """
    Return CDN URLs that bypass HF API auth/rate-limits for public datasets.
    Format: https://huggingface.co/datasets/{repo}/resolve/main/{path}
    """
    base = f"https://huggingface.co/datasets/{repo}/resolve/main"
    return [f"{base}/{p}" for p in file_paths]


def reusable_lightning_studios() -> List[Dict[str, Any]]:
    """
    Lightning pattern: reuse running studios to save quota.
    Returns lightweight metadata for running studios that can be reused.
    """
    if not LIGHTNING_AVAILABLE:
        return []

    try:
        teamspace = Teamspace()
        studios = teamspace.studios or []
        running = []
        for s in studios:
            status = getattr(s, "status", None)
            name = getattr(s, "name", None)
            if status == "Running" and name:
                running.append(
                    {
                        "name": name,
                        "status": status,
                        "id": getattr(s, "id", None),
                        "url": getattr(s, "url", None),
                    }
                )
        return running
    except Exception:
        return []


def build_discovery_report(date_str: str, output_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Build and optionally persist a discovery report for a date.
    """
    if output_dir is None:
        output_dir = Path("discovery") / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    hub = top_hub_insight()
    file_list: List[str] = []
    cdn_urls: List[str] = []
    try:
        file_list = list_hf_date_folder(date_str, HF_PROJECT)
        cdn_urls = cdn_urls_for_files(file_list, HF_PROJECT)
    except Exception as exc:
        # Non-fatal for orchestration; record error for debugging
        hub["hf_list_error"] = str(exc)

    studios = reusable_lightning_studios()

    report = {
        "date": date_str,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "hub": hub,
        "hf": {
            "repo": HF_PROJECT,
            "folder_prefix": "batches/mirror-merged",
            "file_count": len(file_list),
            "files": file_list,
            "cdn_urls_sample": cdn_urls[:10],
        },
        "lightning": {
            "reusable_studios": studios,
            "lightning_available": LIGHTNING_AVAILABLE,
        },
    }

    # Persist
    (output_dir / "file_list.json").write_text(json.dumps(file_list, indent=2))
    (output_dir / "cdn_urls.json").write_text(json.dumps(cdn_urls, indent=2))
    (output_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Vanguard discovery utility")
    parser.add_argument("--date", default=datetime.utcnow().strftime(DATE_FMT), help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--output-dir", type=Path, help="Override output directory")
    args = parser.parse_args()

    try:
        report = build_discovery_report(args.date, args.output_dir)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

### Update: `/opt/axentx/vanguard/backend/orchestrator
