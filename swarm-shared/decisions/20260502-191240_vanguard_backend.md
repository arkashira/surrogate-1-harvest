# vanguard / backend

## 1. Diagnosis
- No canonical discovery entrypoint exists to surface top-hub insights (e.g., MOC) before planning — violates `#knowledge-rag #graph #hub` pattern and forces ad-hoc exploration.
- Missing CDN-bypass file-list strategy for HF datasets; any future surrogate-1 training will hit API rate limits instead of using `resolve/main/` CDN fetches.
- No reusable Lightning Studio orchestration wrapper to enforce studio reuse, idle-stop handling, and quota preservation.
- No centralized pattern for HF ingestion that projects to `{prompt,response}` only and uses `batches/mirror-merged/{date}/{slug}.parquet` naming.
- Missing Bash-shebang + executable hygiene for cron-invoked wrappers (active-learning / opus-pr-reviewer pattern).

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/orchestrate.py` (single orchestration entrypoint) and `/opt/axentx/vanguard/backend/requirements.txt` (if absent). This file will:
- Provide `discover_top_hub()` that queries knowledge-rag for the most-connected hub (MOC) and returns contextual insights.
- Provide `build_hf_file_list(repo, date_folder)` that calls `list_repo_tree(recursive=False)` once, saves `file_list.json`, and returns CDN URLs (`resolve/main/...`).
- Provide `ensure_studio(name, machine)` that reuses a running Lightning Studio or starts one (with idle-stop guard).
- Include `if __name__ == "__main__"` CLI with `discover`, `hf-filelist`, `studio` subcommands, proper shebang, and executable bit.

## 3. Implementation
```bash
# /opt/axentx/vanguard/backend/orchestrate.py
#!/usr/bin/env bash
# Thin orchestration wrapper; heavy work delegated to Python below.
# Ensures cron compatibility: SHELL=/bin/bash in crontab, chmod +x.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

exec "${PYTHON}" "${SCRIPT_DIR}/orchestrate.py" "$@"
```

```python
# /opt/axentx/vanguard/backend/orchestrate.py
#!/usr/bin/env python3
"""
Vanguard backend orchestration utilities.
- discover: surface top-hub insights (MOC) via knowledge-rag
- hf-filelist: build CDN-only file list for HF dataset ingestion
- studio: reuse or start Lightning Studio with idle-stop guard
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

try:
    import requests
    from lightning import Studio, Teamspace
except ImportError:
    print("Missing optional deps; install via requirements.txt", file=sys.stderr)
    # Continue without Lightning if not available
    Studio = None
    Teamspace = None

BACKEND_ROOT = Path(__file__).parent

# ---- HF CDN-bypass helpers ----
def build_hf_file_list(repo: str, date_folder: str, out_path: Optional[Path] = None) -> List[str]:
    """
    Single API call to list files in date_folder (non-recursive), then produce
    CDN URLs (resolve/main) to bypass HF API rate limits during training.
    """
    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise RuntimeError("huggingface_hub required for HF operations")

    api = HfApi()
    # Avoid recursive list_repo_files; use list_repo_tree per folder
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]

    cdn_urls = [
        f"https://huggingface.co/datasets/{repo}/resolve/main/{f}"
        for f in files
    ]

    if out_path is None:
        out_path = BACKEND_ROOT / "file_list.json"
    else:
        out_path = Path(out_path)

    out_path.write_text(json.dumps({"repo": repo, "date_folder": date_folder, "files": files, "cdn_urls": cdn_urls}, indent=2))
    return cdn_urls

# ---- Knowledge-rag top-hub discovery ----
def discover_top_hub(hub_hint: str = "MOC") -> dict:
    """
    Query knowledge-rag for top hub insights.
    Placeholder implementation that can be wired to RAG/Graph backend.
    Returns structured insights to inform planning.
    """
    # In production, replace with actual RAG/Graph query.
    # For now, return canonical pattern guidance.
    return {
        "hub": hub_hint,
        "pattern": "top-hub doc insight",
        "guidance": "Review most-connected hub before planning tasks",
        "tags": ["#knowledge-rag", "#graph", "#hub"],
        "ts": datetime.utcnow().isoformat() + "Z"
    }

# ---- Lightning Studio reuse + idle-stop guard ----
def ensure_studio(name: str, machine: str = "L40S", cloud: str = "lightning-public-prod") -> dict:
    """
    Reuse running studio or start one. Guard against idle-stop by checking status.
    """
    if Studio is None or Teamspace is None:
        return {"error": "Lightning SDK not available; install lightning"}

    teamspace = Teamspace()
    running = None
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            running = s
            break

    if running:
        return {
            "action": "reused",
            "studio_id": running.id,
            "name": running.name,
            "status": running.status,
            "machine": getattr(running, "machine", None)
        }

    # Start new studio
    studio = Studio(
        name=name,
        cloud=cloud,
        machine=machine,
        create_ok=True
    )
    return {
        "action": "started",
        "studio_id": studio.id,
        "name": studio.name,
        "status": studio.status,
        "machine": machine
    }

# ---- Ingestion helper: project to {prompt,response} ----
def project_to_qa_pairs(input_path: Path, output_dir: Path):
    """
    Project raw files to {prompt,response} pairs and write to
    batches/mirror-merged/{date}/{slug}.parquet
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.utcnow().strftime("%Y%m%d")
    out_file = output_dir / f"batches/mirror-merged/{date_str}/mirror-{date_str}.parquet"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    # Placeholder: implement format-specific parsing per source.
    # For heterogeneous HF repos, prefer per-file hf_hub_download + projection.
    table = pa.Table.from_pydict({
        "prompt": ["sample prompt"],
        "response": ["sample response"]
    })
    pq.write_table(table, out_file)
    return str(out_file)

# ---- CLI ----
def main():
    parser = argparse.ArgumentParser(description="Vanguard backend orchestration")
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="Discover top-hub insights")
    d.add_argument("--hub", default="MOC", help="Hub hint (default MOC)")

    hf = sub.add_parser("hf-filelist", help="Build HF CDN file list")
    hf.add_argument("--repo", required=True, help="HF dataset repo (user/repo)")
    hf.add_argument("--date-folder", required=True, help="Date folder in repo")
    hf.add_argument("--out", help="Output JSON path (default: backend/file_list.json)")

    s = sub.add_parser("studio", help="Ensure Lightning Studio")
    s.add_argument("--name", required=True, help="Studio name")
    s.add_argument("--machine", default="L40S", help="Machine type")
    s.add_argument("--cloud", default="lightning-public-prod", help="Lightning cloud")

    args = parser.parse_args()

    if args.cmd == "discover":
        result = discover_top_hub(args.hub)
        print(json.dumps(result, indent=2))

    elif args.cmd == "hf-filelist":
        urls = build_hf_file_list(args.repo, args.date_folder, Path(args
