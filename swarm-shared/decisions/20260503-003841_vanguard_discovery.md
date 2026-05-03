# vanguard / discovery

## 1. Diagnosis
- No persisted `(repo, dateFolder)` file manifest → every training run re-enumerates via authenticated HF API → quota burn and 429 risk.
- Data loader likely uses `load_dataset(streaming=True)` or recursive `list_repo_files` on heterogeneous repos → amplifies rate-limit exposure and invites pyarrow schema errors.
- No CDN-only fetch path in training scripts → authenticated API calls during data loading instead of using public CDN URLs (bypass auth limits).
- Missing executable wrapper hygiene (shebang, `chmod +x`, cron-safe logging) for discovery/ingestion scripts → cron failures and opaque debugging.
- No reuse guard for Lightning Studio in orchestration → quota waste from repeated `create_ok=True` instead of reusing running studios.

## 2. Proposed change
Create `/opt/axentx/vanguard/bin/vanguard-discovery` (executable wrapper) + `/opt/axentx/vanguard/lib/vanguard/discovery.py` (core) that:
- Accepts `REPO` and `DATE_FOLDER` as CLI args.
- Persists a file manifest to `/opt/axentx/vanguard/data/manifests/{repo}/{dateFolder}.json`.
- Uses a single authenticated `list_repo_tree(recursive=False)` per folder to build manifest (Mac-side), then Lightning training consumes manifest and fetches files via CDN-only URLs (zero API calls during training).
- Includes cron-safe logging, proper shebang, and executable permissions.
- Adds a small Studio reuse helper to avoid recreating running studios.

## 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/{bin,lib/vanguard,data/manifests}
```

`/opt/axentx/vanguard/bin/vanguard-discovery`
```bash
#!/usr/bin/env bash
# vanguard-discovery - cron-safe wrapper for discovery/manifest generation
# Usage: vanguard-discovery <repo> <dateFolder> [--force]
# Example: vanguard-dropdown huggingface/datasets 2026-04-29
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export PYTHONPATH="${PROJECT_ROOT}/lib:${PYTHONPATH}"

LOG_DIR="${PROJECT_ROOT}/var/log"
MANIFEST_DIR="${PROJECT_ROOT}/data/manifests"
mkdir -p "${LOG_DIR}" "${MANIFEST_DIR}"

REPO="${1:-}"
DATE_FOLDER="${2:-}"
FORCE="${3:-}"

if [[ -z "${REPO}" || -z "${DATE_FOLDER}" ]]; then
  echo "Usage: $0 <repo> <dateFolder> [--force]" >&2
  exit 1
fi

LOG_FILE="${LOG_DIR}/vanguard-discovery-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] START vanguard-discovery repo=${REPO} dateFolder=${DATE_FOLDER} force=${FORCE}"

MANIFEST_PATH="${MANIFEST_DIR}/${REPO//\//_}/${DATE_FOLDER}.json"
mkdir -p "$(dirname "${MANIFEST_PATH}")"

if [[ -f "${MANIFEST_PATH}" && "${FORCE}" != "--force" ]]; then
  echo "Manifest exists and --force not provided: ${MANIFEST_PATH}"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] SKIP (manifest exists)"
  exit 0
fi

python3 "${PROJECT_ROOT}/lib/vanguard/discovery.py" \
  --repo "${REPO}" \
  --date-folder "${DATE_FOLDER}" \
  --manifest-out "${MANIFEST_PATH}" \
  --force "${FORCE}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE manifest=${MANIFEST_PATH}"
```

`/opt/axentx/vanguard/lib/vanguard/discovery.py`
```python
#!/usr/bin/env python3
"""
Generate a CDN-friendly file manifest for a repo+dateFolder.
Usage:
  python3 discovery.py --repo huggingface/datasets --date-folder 2026-04-29 \
    --manifest-out ./manifests/huggingface_datasets/2026-04-29.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

# HF strategy: use non-recursive folder listing to minimize API calls.
# CDN downloads (resolve/main/...) bypass auth rate limits.
try:
    from huggingface_hub import HfApi, Repository
except ImportError:
    print("ERROR: huggingface_hub not installed. Install with: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

API = HfApi()

def build_manifest(repo: str, date_folder: str, force: bool, out_path: Path) -> Dict:
    """
    Build manifest for repo/date_folder.
    Returns manifest dict and writes to out_path.
    Manifest schema:
    {
      "repo": "user/repo",
      "date_folder": "YYYY-MM-DD",
      "created_utc": 1717020800,
      "files": [
        {"path": "2026-04-29/file1.parquet", "cdn_url": "https://huggingface.co/datasets/user/repo/resolve/main/2026-04-29/file1.parquet"},
        ...
      ]
    }
    """
    if out_path.exists() and not force:
        print(f"Manifest exists (use --force to overwrite): {out_path}")
        with open(out_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # Non-recursive listing for the date folder.
    # If date_folder contains nested folders, caller should invoke per leaf or adapt.
    print(f"Listing repo={repo} path={date_folder} (non-recursive)")
    entries = API.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    files: List[Dict] = []
    base_cdn = f"https://huggingface.co/datasets/{repo}/resolve/main"
    for entry in entries:
        # entry.rfilename is like "2026-04-29/file.parquet"
        rfilename = getattr(entry, "rfilename", None)
        if not rfilename:
            continue
        # Only include files (skip tree entries that are folders)
        if entry.type == "file" or "." in os.path.basename(rfilename):
            files.append({
                "path": rfilename,
                "cdn_url": f"{base_cdn}/{rfilename}"
            })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_utc": int(time.time()),
        "files": files
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"Wrote manifest ({len(files)} files) -> {out_path}")
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN-friendly file manifest for repo+dateFolder")
    parser.add_argument("--repo", required=True, help="HF repo (e.g., huggingface/datasets)")
    parser.add_argument("--date-folder", required=True, help="Folder under dataset repo (e.g., 2026-04-29)")
    parser.add_argument("--manifest-out", required=True, help="Output manifest JSON path")
    parser.add_argument("--force", action="store_true", help="Overwrite existing manifest")
    args = parser.parse_args()

    try:
        build_manifest(
            repo=args.repo,
            date_folder=args.date_folder,
            force=args.force,
            out_path=Path(args.manifest_out)
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

`/opt/axentx/vanguard/lib/vanguard/lightning_reuse.py` (small helper for orchestration)
```python
"""
Lightning Studio reuse helper to avoid quota waste.
"""
from typing import Optional

try:
    from lightning import Studio, Teamspace, Machine
except ImportError:

