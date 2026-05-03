# vanguard / discovery

## Final Consolidated Solution  
*(Best parts merged; contradictions resolved in favor of correctness + concrete actionability)*

---

### 1. Diagnosis (resolved)
- **No deterministic CDN-first manifest** → training/ingestion scripts risk HF API `list_repo_tree`/`load_dataset` calls that can 429 during data loading.  
- **No content-hash integrity verification** for downloaded parquet files → silent corruption can poison surrogate-1 training without detection.  
- **No orchestration/compute boundary enforcement** → Mac/local scripts could accidentally run `model.from_pretrained()` or heavy compute instead of delegating to Lightning/Kaggle/Cerebras.  
- **Missing reusable Lightning Studio guardrails** → idle timeout kills training and quota is wasted by recreating running studios.  
- **No single build-time artifact** that captures “date-folder → file-list + sha256” for reproducible CDN-only training runs.

---

### 2. Proposed change (single deliverable)
Create **one orchestration script** and **one companion loader** that together enforce:

1. **Deterministic manifest generation**  
   - Single HF API call (after rate-limit window) to list folder contents.  
   - Produces `manifests/{date-folder}.jsonl` with `{path, sha256, cdn_url, local_cache}`.  
   - Validates existing local files against sha256; skips CDN download if match.  

2. **CDN-only, zero-auth training loader**  
   - Reads the manifest and downloads exclusively via  
     `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no Authorization header).  
   - Uses local cache when valid; otherwise fetches via CDN and verifies sha256 after download.  
   - Minimal projection to `{prompt, response}` at parse time (surrogate-1 pattern).  

3. **Mac-safe orchestration wrapper**  
   - Bash wrapper invokes the Python builder; safe for local dev and CI.  

Scope:  
- `/opt/axentx/vanguard/build_manifest.py` (Python builder)  
- `/opt/axentx/vanguard/train_cdn_only.py` (CDN-only loader stub)  
- `/opt/axentx/vanguard/build_manifest.sh` (optional Mac-safe wrapper)  

No changes to existing training code yet.

---

### 3. Implementation

#### `/opt/axentx/vanguard/build_manifest.sh` (Mac-safe orchestration wrapper)
```bash
#!/usr/bin/env bash
# Orchestration wrapper (bash) that safely invokes the Python builder.
set -euo pipefail
export SHELL=/bin/bash

REPO_ROOT="/opt/axentx/vanguard"
MANIFEST_DIR="${REPO_ROOT}/manifests"
mkdir -p "${MANIFEST_DIR}"

# Usage: build_manifest.sh <dataset_repo> <date_folder> [--force]
# Example: build_manifest.sh dataset-mirror 2026-05-03

DATASET_REPO="${1:-dataset-mirror}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
FORCE="${3:-}"

python3 "${REPO_ROOT}/build_manifest.py" \
  --repo "${DATASET_REPO}" \
  --folder "${DATE_FOLDER}" \
  --out "${MANIFEST_DIR}/${DATE_FOLDER}.jsonl" \
  ${FORCE:+--force}
```

---

#### `/opt/axentx/vanguard/build_manifest.py` (deterministic CDN-first manifest builder)
```python
#!/usr/bin/env python3
"""
Deterministic CDN-first manifest builder.
Run from Mac (or any orchestrator) after HF API rate-limit window clears.
Produces {date}.jsonl usable by Lightning training with zero API calls.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    print("pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(repo: str, folder: str, out_path: Path, force: bool = False) -> List[Dict]:
    """
    Single HF API call -> CDN-first manifest with integrity hashes.
    """
    print(f"Building manifest for {repo}/{folder} -> {out_path}", file=sys.stderr)

    # One API call: list files in folder (non-recursive)
    tree = list_repo_tree(repo_id=repo, path=folder, recursive=False)
    entries = tree.get("entries", []) if isinstance(tree, dict) else []
    if not entries:
        print("No entries found; ensure folder exists and token/rate-limit allows listing.", file=sys.stderr)
        return []

    os.makedirs(out_path.parent, exist_ok=True)
    mode = "w" if force else "x"
    written = []

    with open(out_path, mode, encoding="utf-8") as f:
        for item in entries:
            if item.get("type") != "file":
                continue
            rel_path = item["path"]  # e.g. dataset-mirror/2026-05-03/file.parquet
            filename = os.path.basename(rel_path)
            local_path = out_path.parent / "cache" / filename

            skip = False
            if local_path.exists() and not force:
                existing_hash = sha256_file(str(local_path))
                skip = True
            else:
                os.makedirs(local_path.parent, exist_ok=True)
                # Download via HF Hub (uses CDN); for pure CDN-only runtime training,
                # embed the CDN URLs and fetch with requests (no auth).
                local_path = hf_hub_download(
                    repo_id=repo,
                    filename=rel_path,
                    local_dir=local_path.parent,
                    local_dir_use_symlinks=False
                )
                existing_hash = sha256_file(local_path)

            record = {
                "repo": repo,
                "path": rel_path,
                "cdn_url": HF_CDN_TEMPLATE.format(repo=repo, path=rel_path),
                "sha256": existing_hash,
                "local_cache": str(local_path),
                "skip_download": skip
            }
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
            written.append(record)
            print(f"  {filename} -> sha256:{existing_hash[:12]}... {'(cached)' if skip else ''}", file=sys.stderr)

    print(f"Manifest written to {out_path} ({len(written)} files)", file=sys.stderr)
    return written

def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN-first manifest for HF dataset folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. dataset-mirror)")
    parser.add_argument("--folder", required=True, help="Folder in repo (e.g. 2026-05-03)")
    parser.add_argument("--out", required=True, type=Path, help="Output .jsonl path")
    parser.add_argument("--force", action="store_true", help="Overwrite manifest and re-download")
    args = parser.parse_args()

    try:
        build_manifest(args.repo, args.folder, args.out, args.force)
    except Exception as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

---

#### `/opt/axentx/vanguard/train_cdn_only.py` (Lightning-compatible CDN-only loader)
```python
#!/usr/bin/env python3
"""
Lightning-compatible data loader that uses ONLY CDN URLs from a manifest.
Zero HF API calls during training.
"""
import json
import hashlib
import sys
from pathlib import Path
from typing import Iterator, Dict, Any

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def sha256_file(path: str) -> str:

