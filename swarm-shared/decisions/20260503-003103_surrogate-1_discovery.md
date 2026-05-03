# surrogate-1 / discovery

## Implementation Plan — CDN-first snapshot + zero-HF-API ingestion

**Goal**: Eliminate HF API rate-limit risk during training by producing a deterministic file manifest once (on the Mac orchestrator) and having Lightning training fetch exclusively via CDN URLs.

**Scope** (fits <2h):
- Add `scripts/make_snapshot.py` (Mac orchestrator) → lists `axentx/surrogate-1-training-pairs` tree (non-recursive per date folder), saves `snapshot-{date}.json` with CDN URLs + metadata.
- Add `data/cdn_stream.py` (Lightning training side) → reads snapshot, yields `(prompt, response)` via direct `requests`/`urllib` CDN fetch (zero HF API calls).
- Add `scripts/ci_test_snapshot.sh` → quick smoke test that snapshot resolves and yields ≥1 valid record.
- Update `README.md` with usage and the HF API strategy note.

**Why this is highest-value**: removes the 429/1000-per-5min risk during training loops, enables reproducible training runs, and costs nothing to run.

---

### 1) Create snapshot script (Mac orchestrator)

`scripts/make_snapshot.py`

```python
#!/usr/bin/env python3
"""
Create a CDN-only snapshot for axentx/surrogate-1-training-pairs.

Usage:
    python scripts/make_snapshot.py --repo axentx/surrogate-1-training-pairs \
        --out snapshots/snapshot-2026-05-03.json

Notes:
- Uses HF Hub tree API (non-recursive) per top-level folder to avoid
  recursive pagination on large repos.
- CDN URLs are https://huggingface.co/datasets/{repo}/resolve/main/{path}
  and do NOT count against API rate limits.
- Output is deterministic (sorted) so training runs are reproducible.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: install huggingface_hub (pip install huggingface_hub)")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_folders(api: HfApi, repo: str) -> List[str]:
    """Return top-level folder names (expected YYYY-MM-DD)."""
    tree = api.list_repo_tree(repo=repo, path="", recursive=False)
    folders = [
        item.rfilename.rstrip("/")
        for item in tree
        if item.type == "directory"
    ]
    # Keep only date-like folders to avoid picking stray metadata dirs
    date_folders = [f for f in folders if _is_date_folder(f)]
    date_folders.sort()
    return date_folders

def _is_date_folder(name: str) -> bool:
    # Accept YYYY-MM-DD or YYYYMMDD variants used in the repo
    import re
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", name) or re.fullmatch(r"\d{8}", name))

def list_files_in_folder(api: HfApi, repo: str, folder: str) -> List[Dict]:
    """List files in folder (non-recursive). Return dicts with CDN URL + metadata."""
    tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    files = []
    for item in tree:
        if item.type == "file":
            path = item.rfilename
            files.append(
                {
                    "repo": repo,
                    "path": path,
                    "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path),
                    "size": getattr(item, "size", None),
                    "lfs": getattr(item, "lfs", None) is not None,
                }
            )
    # Deterministic ordering
    files.sort(key=lambda x: x["path"])
    return files

def build_snapshot(repo: str, folders: List[str], api: HfApi) -> Dict:
    snapshot = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "strategy": "cdn-only",
        "note": "CDN URLs bypass HF API auth/rate-limits during training.",
        "folders": {},
    }

    for folder in folders:
        files = list_files_in_folder(api, repo, folder)
        snapshot["folders"][folder] = files

    # Flattened file list for convenience
    all_files: List[Dict] = []
    for folder, files in snapshot["folders"].items():
        for f in files:
            all_files.append(f)
    snapshot["all_files"] = all_files
    snapshot["total_files"] = len(all_files)
    return snapshot

def main() -> None:
    parser = argparse.ArgumentParser(description="Create CDN snapshot for HF dataset repo.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--out", default="snapshots/snapshot-latest.json")
    parser.add_argument("--date-out", action="store_true", help="Use date in filename")
    args = parser.parse_args()

    api = HfApi()

    print(f"Listing folders in {args.repo} ...")
    folders = list_date_folders(api, args.repo)
    if not folders:
        print("WARNING: no date folders found; listing root files instead.")
        folders = [""]

    snapshot = build_snapshot(args.repo, folders, api)

    out_path = Path(args.out)
    if args.date_out or "{date}" in args.out:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        out_path = Path(f"snapshots/snapshot-{today}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(snapshot, fp, indent=2, sort_keys=True)

    print(f"Snapshot written to {out_path} ({snapshot['total_files']} files)")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x scripts/make_snapshot.py
```

---

### 2) CDN streamer for Lightning training (zero HF API)

`data/cdn_stream.py`

```python
"""
CDN-only streaming loader for surrogate-1 training pairs.

Usage:
    from data.cdn_stream import CdnShardLoader

    loader = CdnShardLoader(snapshot_path="snapshots/snapshot-2026-05-03.json")
    for record in loader.stream_records():
        # record = {"prompt": ..., "response": ..., "source_file": ...}
        ...

Behavior:
- Reads snapshot JSON produced by scripts/make_snapshot.py.
- Streams each file via CDN URL (no HF API calls).
- Projects to {prompt, response} only at parse time.
- Supports common formats: JSONL, JSON (array), parquet (via pyarrow).
- Retries transient HTTP errors with exponential backoff.
"""
import json
import logging
import time
from pathlib import Path
from typing import Dict, Generator, List, Optional

import numpy as np
import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter, Retry

logger = logging.getLogger(__name__)

CDN_TIMEOUT = (60, 300)  # connect, read

class CdnShardLoader:
    def __init__(
        self,
        snapshot_path: str,
        max_retries: int = 5,
        backoff_factor: float = 1.5,
        status_forcelist: Optional[List[int]] = None,
    ):
        self.snapshot_path = Path(snapshot_path)
        self.session = self._make_session(max_retries, backoff_factor, status_forcelist)
        self._snapshot: Optional[Dict] = None

    def _make_session(self, max_retries: int, backoff_factor: float, status_forcelist):
        sess = requests.Session()
        retries = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=status_forcelist or [429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
        sess.mount("https://", HTTPAdapter(max_retries=retries
