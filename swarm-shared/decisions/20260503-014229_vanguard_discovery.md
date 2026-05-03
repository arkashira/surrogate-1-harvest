# vanguard / discovery

## Final Consolidated Solution

### 1. Diagnosis (merged)
- **Root cause**: Frontend and training scripts trigger authenticated `list_repo_tree` on every selection/launch, exhausting the 1000/5min HF API quota and causing 429s.
- **Missing artifact**: No persisted `(repo, dateFolder) → file-list` manifest, forcing repeated enumeration and blocking reproducible, CDN-only training.
- **Data-loading flaw**: Runtime use of `load_dataset(..., streaming=True)` or `list_repo_files` fails on heterogeneous schemas (PyArrow CastError) and incurs auth’d API calls.
- **CDN bypass absent**: Training does not use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` fetches that avoid API limits entirely.
- **Orchestration gaps**: No pre-flight validation of CDN availability/file integrity and no guardrails against concurrent Studio runs or stale manifests.

### 2. Proposed Change (merged)
Add a discovery-side manifest generator, a CDN-only data loader, and lightweight orchestration guardrails for Surrogate-1 training:
- New: `/opt/axentx/vanguard/scripts/discover_manifest.py`
- New: `/opt/axentx/vanguard/scripts/validate_cdn.py`
- New: `/opt/axentx/vanguard/scripts/run_discovery.sh`
- New: `/opt/axentx/vanguard/manifests/` (directory)
- Modified: `/opt/axentx/vanguard/training/train.py` (data-loading section)
Scope: single-date folder enumeration → JSON manifest + CDN validation → embed in training → CDN-only fetches + concurrency guardrails.

### 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/{scripts,training,manifests}
```

#### `/opt/axentx/vanguard/scripts/discover_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate and persist file-list manifest for (repo, dateFolder).
Usage:
  python discover_manifest.py \
    --repo datasets/axentx/surrogate-1 \
    --date 2026-04-29 \
    --out manifests/surrogate-1_2026-04-29_file-list.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

HF_TOKEN = os.getenv("HF_TOKEN", "")

def build_manifest(repo_id: str, date_folder: str, out_path: Path):
    print(f"Listing {repo_id}/{date_folder} (non-recursive)...")
    try:
        tree = list_repo_tree(
            repo_id=repo_id,
            path=date_folder,
            recursive=False,
            repo_type="dataset",
            token=HF_TOKEN or None,
        )
    except Exception as e:
        print(f"HF API error: {e}")
        sys.exit(1)

    files = sorted([
        f"{date_folder}/{node.path}" for node in tree
        if node.type == "file" and node.path.lower().endswith((".parquet", ".jsonl", ".json"))
    ])

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "count": len(files),
        "strategy": "cdn_only",
        "note": "Use resolve/main/ URLs to bypass HF API rate limits during training."
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate file-list manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/axentx/surrogate-1)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    build_manifest(args.repo, args.date, Path(args.out))
```

#### `/opt/axentx/vanguard/scripts/validate_cdn.py`
```python
#!/usr/bin/env python3
"""
Validate CDN availability and basic integrity for files in a manifest.
Usage:
  python validate_cdn.py \
    --manifest manifests/surrogate-1_2026-04-29_file-list.json \
    --repo datasets/axentx/surrogate-1 \
    --workers 8
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

HF_DATASETS_BASE = "https://huggingface.co/datasets"
TIMEOUT = 30

def build_cdn_url(repo_id: str, rel_path: str) -> str:
    return f"{HF_DATASETS_BASE}/{repo_id}/resolve/main/{rel_path}"

def check_file(url: str) -> bool:
    try:
        r = requests.head(url, allow_redirects=True, timeout=TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False

def validate(manifest_path: Path, repo_id: str, workers: int):
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    repo_ok = manifest.get("repo_id") == repo_id
    if not repo_ok:
        print(f"Manifest repo_id mismatch: expected {repo_id}, got {manifest.get('repo_id')}")
        sys.exit(1)

    urls = [build_cdn_url(repo_id, f) for f in manifest["files"]]
    ok = 0
    failed = []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_url = {ex.submit(check_file, url): url for url in urls}
        for fut in as_completed(future_to_url):
            url = future_to_url[fut]
            if fut.result():
                ok += 1
            else:
                failed.append(url)

    print(f"CDN validation: {ok}/{len(urls)} OK")
    if failed:
        print("Failed URLs:")
        for url in failed[:20]:
            print("  " + url)
        if len(failed) > 20:
            print(f"  ... and {len(failed) - 20} more")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate CDN availability for manifest files.")
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/axentx/surrogate-1)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel HEAD requests")
    args = parser.parse_args()

    validate(Path(args.manifest), args.repo, args.workers)
```

#### `/opt/axentx/vanguard/scripts/run_discovery.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
# Generate manifest and validate CDN availability.
# Example: generate manifest for a specific date folder.

REPO="datasets/axentx/surrogate-1"
DATEFOLDER="${1:-$(date -u +%Y-%m-%d)}"
OUT="manifests/surrogate-1_${DATEFOLDER}_file-list.json"

echo "Running discovery for ${REPO}/${DATEFOLDER}..."
python scripts/discover_manifest.py --repo "$REPO" --date "$DATEFOLDER" --out "$OUT"

echo "Validating CDN availability..."
python scripts/validate_cdn.py --manifest "$OUT" --repo "$REPO" --workers 8

echo "Done. Manifest ready: $OUT"
```
```bash
chmod +x /opt/axentx/vanguard/scripts/run_discovery.sh
```

#### `/opt/axentx/vanguard/training/train.py` (data-loading section)
```python
import json
import os
from pathlib import Path
from typing import List, Optional


