# vanguard / discovery

## 1. Diagnosis
- No persistent file manifest: repeated `list_repo_tree`/`load_dataset` calls will trigger HF API 429s during discovery/training iterations.
- Lightning Studio reuse missing: each run risks quota burn via redundant create/idle/stop cycles.
- Schema drift exposure: discovery may ingest heterogeneous repo files without projecting to `{prompt,response}` early, risking downstream `pyarrow.CastError`.
- No CDN bypass strategy: discovery/training scripts likely rely on `load_dataset`/`hf_hub_download` paths that count against API rate limits.
- Missing runbook for rate-limit recovery: no backoff/retry or file-list snapshot mechanism for multi-hour discovery jobs.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/manifest.py` (new) and update `/opt/axentx/vanguard/discovery/run_discovery.py` (or create if absent) to:
- Snapshot repo tree once per date folder and persist to `manifests/{date}/files.json`.
- Embed that manifest in downstream data loading so training/discovery uses CDN-only fetches.
- Reuse a running Lightning Studio by name instead of recreating.
- Project heterogeneous files to `{prompt,response}` at parse time.

## 3. Implementation

```bash
# Ensure directory structure
mkdir -p /opt/axentx/vanguard/{discovery,manifests}
cd /opt/axentx/vanguard/discovery
```

### manifest.py
```python
#!/usr/bin/env python3
"""
Snapshot HF repo tree for a date folder and produce a CDN-ready manifest.
Usage:
    python manifest.py --repo datasets/mycorp/vanguard --date 2026-05-02 --out manifests/2026-05-02/files.json
"""
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict

from huggingface_hub import HfApi, RepositoryNotFoundError

HF_API_RATE_LIMIT_RESET_BUFFER = 360  # seconds

def snapshot_repo_files(repo_id: str, date_folder: str, out_path: Path) -> List[Dict]:
    api = HfApi()
    try:
        # Non-recursive top-level of the date folder to minimize pagination
        entries = api.list_repo_tree(repo_id, path=date_folder, recursive=False)
    except RepositoryNotFoundError:
        raise SystemExit(f"Repo not found: {repo_id}")
    except Exception as exc:
        # If 429, wait and retry once
        if getattr(exc, "status_code", None) == 429:
            wait = HF_API_RATE_LIMIT_RESET_BUFFER
            print(f"Rate limited (429). Waiting {wait}s before retry.")
            time.sleep(wait)
            entries = api.list_repo_tree(repo_id, path=date_folder, recursive=False)
        else:
            raise

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        # CDN URL bypasses API auth checks and rate limits for downloads
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{entry.path}"
        files.append({
            "path": entry.path,
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "snapshot_ts": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote manifest with {len(files)} files -> {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snapshot HF repo tree for CDN manifest.")
    parser.add_argument("--repo", required=True, help="HF dataset repo id (e.g. datasets/owner/name)")
    parser.add_argument("--date", required=True, help="Date folder in repo (e.g. 2026-05-02)")
    parser.add_argument("--out", required=True, help="Output JSON path (e.g. manifests/2026-05-02/files.json)")
    args = parser.parse_args()
    snapshot_repo_files(args.repo, args.date, Path(args.out))
```

### discovery/run_discovery.py (minimal starter)
```python
#!/usr/bin/env python3
"""
Discovery runner:
- Reuses running Lightning Studio when available.
- Loads manifest and streams files via CDN (no HF API during data load).
- Projects heterogeneous files to {prompt, response} at parse time.
"""
import json
import os
import sys
from pathlib import Path
from typing import Iterator, Dict

import requests
from lightning import Fabric, LightningModule, Trainer
from lightning.pytorch.loggers import CSVLogger
from lightning.pytorch.strategies import FSDPStrategy

# Optional: import surrogate-1 model components here
# from vanguard.model import SurrogateModel

MANIFEST_PATH = Path(os.getenv("VANGUARD_MANIFEST", "manifests/2026-05-02/files.json"))
LIGHTNING_STUDIO_NAME = os.getenv("LIGHTNING_STUDIO_NAME", "vanguard-discovery")

def iter_cdn_records(manifest_path: Path) -> Iterator[Dict]:
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    for item in manifest["files"]:
        url = item["cdn_url"]
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            print(f"Failed to fetch {url}: {exc}")
            continue

        # Project to {prompt, response} at parse time (schema-agnostic)
        # Replace with your actual parser per file type / repo layout
        raw = resp.text
        record = project_to_prompt_response(raw, source_path=item["path"])
        if record:
            yield record

def project_to_prompt_response(raw: str, source_path: str) -> Dict | None:
    """
    Minimal projection: adapt to your repo's file formats.
    Returns {"prompt": ..., "response": ...} or None if unparseable.
    """
    # Example heuristic: if JSONL, parse and map fields.
    # If markdown/code, heuristics or model-based extraction.
    # Keep attribution in filename pattern, not extra columns.
    raw = raw.strip()
    if not raw:
        return None

    # Placeholder: treat whole file as prompt, empty response for discovery labeling
    return {"prompt": raw, "response": ""}

def reuse_or_create_studio():
    """
    Reuse running studio to save quota. If not running, start one.
    Requires lightning[studio] installed and configured credentials.
    """
    try:
        from lightning.fabric.plugins import LightningStudioPlugin
        from lightning.fabric.studios import Studio, Teamspace
    except ImportError:
        print("Lightning Studio plugin not available; skipping studio reuse.")
        return None

    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == LIGHTNING_STUDIO_NAME and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s

    print(f"No running studio named {LIGHTNING_STUDIO_NAME}; creating one (L40S).")
    # Free tier fallback: L40S in lightning-public-prod
    studio = Studio(
        name=LIGHTNING_STUDIO_NAME,
        create_ok=True,
        machine="L40S",
        cloud="lightning-public-prod",
    )
    return studio

def main():
    if not MANIFEST_PATH.exists():
        print(f"Manifest missing: {MANIFEST_PATH}")
        print("Run manifest.py first to snapshot repo files.")
        sys.exit(1)

    # Optional: reuse studio for orchestrated compute
    studio = reuse_or_create_studio()
    if studio and studio.status != "running":
        print("Studio not running; starting...")
        studio.start(machine="L40S")

    # Lightweight discovery loop (stream from CDN)
    records = list(iter_cdn_records(MANIFEST_PATH))
    print(f"Discovered {len(records)} records from manifest.")

    # Example: quick local training step (or export for Lightning Studio training)
    # Replace with your actual model/train step
    if
