# vanguard / discovery

# 1. Diagnosis

- No persisted `(repo, dateFolder)` file manifest — every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Data loader likely uses recursive enumeration or `load_dataset(streaming=True)` on heterogeneous repos, triggering pyarrow schema errors and extra API calls.
- Training script probably recomputes file lists at runtime instead of embedding a static list, preventing true CDN-only fetches during training.
- No executable wrapper or cron-safe logging for discovery/ingest jobs (risk of silent failures and non-portable invocation).
- Missing fallback to CDN bypass pattern (`resolve/main/` direct downloads) when API limits are hit.

# 2. Proposed change

Create a small, reusable discovery utility that:
- Snapshots a HuggingFace dataset repo’s date folder into a local JSON manifest (non-recursive, one API call per folder).
- Generates a `train_filelist.json` and a minimal `train_cdn.py` loader that performs only CDN fetches (zero authenticated API calls during training).
- Adds an executable Bash wrapper (`bin/vanguard-discovery`) with proper shebang, logging, and cron-safe behavior.

Scope:
- New file: `/opt/axentx/vanguard/bin/vanguard-discovery` (executable)
- New file: `/opt/axentx/vanguard/lib/vanguard/discovery.py`
- New file: `/opt/axentx/vanguard/train_cdn.py` (minimal loader stub for Lightning)

# 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/{bin,lib/vanguard,data}
```

## 3.1 lib/vanguard/discovery.py

```python
#!/usr/bin/env python3
"""
Snapshot HF dataset repo folder into a CDN-only file manifest.
Usage:
  python3 discovery.py <repo_id> <date_folder> [--out OUT_JSON]

Example:
  python3 discovery.py datasets/mycorp/vanguard 2026-05-03 --out data/filelist.json
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("ERROR: missing huggingface_hub. Install with: pip install huggingface_hub")
    sys.exit(1)

HF_API_RETRY_WAIT = 360  # seconds after 429

def snapshot_repo_folder(repo_id: str, folder: str, out_path: Path) -> Dict:
    """
    Non-recursive tree listing for one folder.
    Returns manifest with repo, folder, ts, and file entries.
    """
    started = datetime.utcnow().isoformat() + "Z"
    attempt = 0
    max_attempts = 3

    while attempt < max_attempts:
        try:
            # recursive=False => single API call, no pagination explosion
            tree = list_repo_tree(repo_id=repo_id, folder=folder, recursive=False)
            files = [
                {
                    "path": node.path,
                    "size": getattr(node, "size", None),
                    "lfs": getattr(node, "lfs", None),
                }
                for node in tree if not node.path.endswith("/")
            ]
            manifest = {
                "repo_id": repo_id,
                "folder": folder.rstrip("/"),
                "created_utc": started,
                "count": len(files),
                "files": files,
            }
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(manifest, indent=2))
            print(f"OK: wrote {len(files)} entries to {out_path}")
            return manifest
        except Exception as exc:
            attempt += 1
            if "429" in str(exc) or "rate limit" in str(exc).lower():
                wait = HF_API_RETRY_WAIT
                print(f"RATE LIMITED (429). Waiting {wait}s before retry ({attempt}/{max_attempts})...")
                time.sleep(wait)
                continue
            print(f"ERROR: snapshot failed: {exc}", file=sys.stderr)
            if attempt >= max_attempts:
                raise
    raise RuntimeError("Exhausted retries for repo listing")

def main() -> None:
    parser = argparse.ArgumentParser(description="Create CDN-only file manifest for HF dataset folder")
    parser.add_argument("repo_id", help="HF repo id (e.g. datasets/mycorp/vanguard)")
    parser.add_argument("folder", help="Folder path inside repo (e.g. 2026-05-03 or batches/mirror-merged/2026-05-03)")
    parser.add_argument("--out", default="data/filelist.json", help="Output JSON path")
    args = parser.parse_args()

    out_path = Path(args.out).expanduser().resolve()
    snapshot_repo_folder(args.repo_id, args.folder, out_path)

if __name__ == "__main__":
    main()
```

## 3.2 bin/vanguard-discovery

```bash
#!/usr/bin/env bash
#
# vanguard-discovery
# Cron-safe wrapper to snapshot HF dataset folder into CDN manifest.
#
# Usage (cron):
#   SHELL=/bin/bash
#   0 2 * * * cd /opt/axentx/vanguard && ./bin/vanguard-discovery datasets/mycorp/vanguard 2026-05-03 >> logs/discovery.log 2>&1
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${REPO_ROOT}/logs"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "${LOG_DIR}"
exec >> "${LOG_DIR}/discovery-${TIMESTAMP}.log" 2>&1

echo "=== vanguard-discovery start ${TIMESTAMP} ==="
echo "PWD: ${REPO_ROOT}"
echo "ARGS: $*"

cd "${REPO_ROOT}"

# Ensure Python uses repo venv if present, else system
if [[ -f "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  PYTHON="python3"
fi

"${PYTHON}" lib/vanguard/discovery.py "$@"

echo "=== vanguard-discovery done ==="
```

Make it executable:

```bash
chmod +x /opt/axentx/vanguard/bin/vanguard-discovery
chmod +x /opt/axentx/vanguard/lib/vanguard/discovery.py
```

## 3.3 train_cdn.py (minimal CDN-only loader stub)

```python
#!/usr/bin/env python3
"""
Lightning-friendly CDN-only dataset loader.
Embed the file list produced by discovery.py and fetch via CDN URLs
(https://huggingface.co/datasets/{repo}/resolve/main/{path}) with zero
authenticated HF API calls during training.

Usage in Lightning Studio:
  from train_cdn import CdnParquetDataset
  ds = CdnParquetDataset(manifest_path="data/filelist.json")
  # use with DataLoader or Lightning DataModule
"""
import json
from pathlib import Path
from typing import List, Dict
import requests
from torch.utils.data import IterableDataset

class CdnParquetDataset(IterableDataset):
    def __init__(self, manifest_path: str, repo_id: str = None):
        manifest_path = Path(manifest_path).expanduser().resolve()
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.repo_id = repo_id or manifest["repo_id"]
        self.folder = manifest["folder"]
        self.files: List[Dict] = manifest["files"]
        self.base_url = f"https://huggingface.co/datasets/{self.repo_id}/resolve/main"

    def _cdn_url(self, rel_path: str) -> str:
        # folder may already include subpath; files stored with full repo-relative path
        return f"{self.base_url}/{rel_path}"

    def __iter__(self):
        for entry in self.files:
            url = self._cdn_url(entry["path"])
            # Lightweight example: stream parquet bytes and project {prompt,response}
            # Replace with actual parsing logic (pyarrow, pandas, etc.)
            yield {"url": url, "size": entry["size"], "path": entry["path"]}

def example_usage():
    ds = CdnParquetDataset("data/filelist.json")
    for item
