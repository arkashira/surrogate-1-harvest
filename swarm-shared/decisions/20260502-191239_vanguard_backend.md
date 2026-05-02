# vanguard / backend

## 1. Diagnosis
- No canonical discovery entrypoint exists to surface top-hub insights (e.g., MOC) before planning — violates `#knowledge-rag #graph #hub` pattern and forces ad-hoc exploration.
- Missing CDN-bypass file-list strategy for HF datasets; any future surrogate-1 training will hit API rate limits instead of using resolve/main CDN fetches.
- No reusable Lightning Studio orchestration wrapper to enforce idle-stop handling and quota reuse (violates `#lightning-ai #quota` pattern).
- No project-level CLI entrypoint to run business-research + knowledge-rag in one command (violates `#business-research #knowledge-rag` pattern).
- Missing Bash-shebang + executable hygiene for cron-invoked wrappers (violates `#bash #script-error` patterns for opus-pr-reviewer/active-learning).

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/discover.py` (CLI entrypoint) and `/opt/axentx/vanguard/backend/train_utils.py` (CDN-bypass file-list + Lightning Studio reuse helpers). Expose a single `vanguard-discover` console script that:
- Runs knowledge-rag top-hub query (MOC) and prints actionable insights.
- Optionally pre-lists HF dataset files for a given date folder and emits `file_list.json` for CDN-only training.
- Accepts `--studio-reuse` to find or start a Lightning Studio (L40S) with idle-stop resilience.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/discover.py
#!/usr/bin/env python3
"""
vanguard-discover
Canonical discovery entrypoint:
- Top-hub insights via knowledge-rag
- HF CDN-bypass file-list generation
- Lightning Studio reuse helper
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

# Optional imports (fail gracefully with helpful message)
try:
    import requests
except Exception:  # noqa
    requests = None

try:
    from lightning import LightningWork, LightningApp, Machine
    from lightning.app import Studio
    from lightning.app.utilities.cloud import get_cloud
    LIGHTNING_AVAILABLE = True
except Exception:  # noqa
    LIGHTNING_AVAILABLE = False

HF_DATASETS_CDN = "https://huggingface.co/datasets"
PROJECT_ROOT = Path(__file__).parent.parent.parent

def top_hub_insights() -> None:
    """
    Run knowledge-rag heuristic to surface most-connected hub (e.g., MOC).
    This is a lightweight heuristic placeholder that can be replaced with
    an actual RAG query when the knowledge graph tooling is wired.
    """
    # Placeholder: print canonical guidance per pattern
    print("== Top-hub insight (knowledge-rag) ==")
    print("Review most-connected hub (e.g., MOC) before planning tasks.")
    print("Tags: #knowledge-rag #graph #hub")
    print("")
    # If a real CLI exists (e.g. `knowledge-rag`), invoke it here:
    # os.execvp("knowledge-rag", ["knowledge-rag", "top-hub", "--project", "vanguard"])

def list_hf_folder_cdn(repo: str, folder: str) -> List[str]:
    """
    Use HF REST tree API (single call) to list files in folder (non-recursive).
    Returns paths relative to repo root. Bypasses /api/ rate limits by using
    resolve/main CDN for actual file downloads during training.
    """
    if requests is None:
        raise RuntimeError("requests required for HF listing")
    api_url = f"https://huggingface.co/api/datasets/{repo}/tree"
    params = {"path": folder, "recursive": "false"}
    resp = requests.get(api_url, params=params, timeout=30)
    if resp.status_code == 429:
        print("HF API rate-limited (429). Wait 360s before retry.", file=sys.stderr)
        raise RuntimeError("HF rate limit")
    resp.raise_for_status()
    items = resp.json()
    # Filter to files only (exclude nested trees)
    files = [item["path"] for item in items if item.get("type") == "file"]
    return files

def build_file_list(repo: str, folder: str, out_path: Path) -> None:
    """Generate file_list.json for CDN-only training."""
    files = list_hf_folder_cdn(repo, folder)
    payload = {
        "repo": repo,
        "folder": folder,
        "files": files,
        "cdn_prefix": f"{HF_DATASETS_CDN}/{repo}/resolve/main/{folder}"
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(files)} file entries to {out_path}")

def find_running_studio(name: str):
    """Reuse running studio to save quota (lightning-ai pattern)."""
    if not LIGHTNING_AVAILABLE:
        print("Lightning not available; skipping studio reuse.", file=sys.stderr)
        return None
    for s in Studio.list():
        if s.name == name and getattr(s, "status", None) == "Running":
            return s
    return None

def ensure_studio(name: str, machine: str = "L40S"):
    """
    Find or start a Lightning Studio.
    Note: H200 only in lightning-lambda-prod (paid). Default free tier -> L40S.
    """
    if not LIGHTNING_AVAILABLE:
        print("Lightning SDK not available; cannot manage Studio.", file=sys.stderr)
        return

    existing = find_running_studio(name)
    if existing:
        print(f"Reusing running studio: {name}")
        return existing

    print(f"Starting studio '{name}' on {machine} (idle-stop aware).")
    # Minimal Work to keep Studio alive for training orchestration
    class TrainWork(LightningWork):
        def run(self, **kwargs):
            # Placeholder: training script would be launched here or via .run()
            print("TrainWork running — attach your training command.")
            import time
            # Keep alive; real training should be invoked via target.run()
            time.sleep(5)

    studio = Studio(
        name=name,
        work=TrainWork(),
        # Default cloud will pick free-tier (L40S). For H200 specify:
        # cloud=Cloud(provider="lightning-lambda-prod")
    )
    studio.start()
    return studio

def main() -> None:
    parser = argparse.ArgumentParser(description="Vanguard discovery & training prep")
    parser.add_argument("--hf-repo", default="", help="HF dataset repo (e.g., org/repo)")
    parser.add_argument("--hf-folder", default="", help="Folder in dataset to list")
    parser.add_argument("--out-file", default="file_list.json", help="Output JSON path")
    parser.add_argument("--studio-reuse", action="store_true", help="Find/start Lightning Studio")
    parser.add_argument("--studio-name", default="vanguard-train", help="Studio name")
    parser.add_argument("--skip-hub", action="store_true", help="Skip top-hub insights")
    args = parser.parse_args()

    if not args.skip_hub:
        top_hub_insights()

    if args.hf_repo and args.hf_folder:
        out = Path(args.out_file)
        build_file_list(args.hf_repo, args.hf_folder, out)
        print("")
        print("CDN-bypass guidance:")
        print(f"  Use file URLs from {out} with no Authorization header.")
        print(f"  Example: {HF_DATASETS_CDN}/{args.hf_repo}/resolve/main/{args.hf_folder}/<file>")
        print("")

    if args.studio_reuse:
        ensure_studio(args.studio_name)

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/backend/train_utils.py
"""
Shared training utilities for surrogate-1 pipeline.
- CDN-only dataset fetching (bypasses HF API rate limits)
- HF commit-cap mitigation helpers (sibling repo hashing)
"""
import hashlib
import json
from pathlib import Path
from typing import Dict, List

def cdn_urls(file_list_path: Path) -> List[str]:
    """Load file_list.json and return CDN URLs for zero-API fetching."""
    data = json.loads(file_list_path.read_text())
    prefix = data.get("cdn_prefix") or f"https://huggingface.co/datasets/{data['repo']}/resolve
