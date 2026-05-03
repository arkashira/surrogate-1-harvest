# vanguard / discovery

## 1. Diagnosis
- No deterministic CDN-first manifest exists; training/ingestion scripts likely still call `list_repo_tree` or `load_dataset` at runtime, risking 429s and non-reproducible runs.
- Missing content-addressed file list with pinned ordering and integrity (SHA-256) for the date-partitioned folders that will be used during training.
- No lightweight discovery script to produce the manifest on the Mac orchestration host (single API call) and embed it into training jobs.
- Absence of a small verification path to confirm CDN URLs resolve and match expected hashes before long-running training starts.
- No documented pattern in repo for “generate once, embed, CDN-only” workflow; teams may re-invent brittle recursive listing.

## 2. Proposed change
Add a single, reusable discovery script and a lockfile pattern:
- File: `/opt/axentx/vanguard/scripts/discover_and_lock.py`
- Scope: ~120 lines; CLI accepts `--repo`, `--date-folder`, `--out-manifest`, `--out-lock`. Produces deterministic JSON manifest and a SHA-256 lockfile.
- Consumers: training scripts read the embedded manifest and fetch via CDN only; CI can verify lockfile before launch.

## 3. Implementation
Create script and example usage.

```bash
# Ensure scripts directory exists
mkdir -p /opt/axentx/vanguard/scripts
```

```python
# /opt/axentx/vanguard/scripts/discover_and_lock.py
#!/usr/bin/env python3
"""
CDN-first discovery for HF datasets.
Generates deterministic manifest + SHA-256 lockfile for a repo+date-folder.
Usage:
  python discover_and_lock.py \
    --repo datasets/myorg/vanguard-ingest \
    --date-folder 2026-05-03 \
    --out-manifest manifest.json \
    --out-lock lock.sha256
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import List, Dict

import requests

HF_API_BASE = "https://huggingface.co/api"
CDN_BASE = "https://huggingface.co/datasets"
RETRY_WAIT = 360  # seconds after 429

def list_folder(repo: str, path: str) -> List[Dict]:
    """Single non-recursive directory listing. Returns items with 'path' and 'type'."""
    url = f"{HF_API_BASE}/datasets/{repo}/tree"
    params = {"path": path, "recursive": "false"}
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code == 429:
        print("Rate limited (429). Waiting before retry...", file=sys.stderr)
        time.sleep(RETRY_WAIT)
        return list_folder(repo, path)
    resp.raise_for_status()
    return resp.json()

def build_manifest(repo: str, date_folder: str) -> List[Dict]:
    """Return deterministic manifest for files under date_folder (non-recursive)."""
    items = list_folder(repo, date_folder)
    # Keep only files (exclude subfolders) and sort by path for determinism
    files = [i for i in items if i.get("type") == "file"]
    files.sort(key=lambda x: x["path"])

    manifest = []
    for f in files:
        rel = f["path"]
        cdn_url = f"{CDN_BASE}/{repo}/resolve/main/{rel}"
        manifest.append(
            {
                "repo": repo,
                "path": rel,
                "cdn_url": cdn_url,
                "size": f.get("size"),
                "lfs": f.get("lfs", {}).get("oid") is not None,
            }
        )
    return manifest

def compute_lock(manifest: List[Dict]) -> str:
    """Deterministic SHA-256 over canonical JSON of manifest."""
    canonical = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()

def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-first HF dataset discovery")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. user/repo)")
    parser.add_argument("--date-folder", required=True, help="Folder path in repo")
    parser.add_argument("--out-manifest", default="manifest.json", help="Output manifest path")
    parser.add_argument("--out-lock", default="lock.sha256", help="Output lockfile path")
    args = parser.parse_args()

    print(f"Discovering {args.repo}/{args.date_folder} (non-recursive)...")
    manifest = build_manifest(args.repo, args.date_folder)
    lock = compute_lock(manifest)

    Path(args.out_manifest).write_text(json.dumps(manifest, indent=2) + "\n")
    Path(args.out_lock).write_text(f"{lock}  {args.out_manifest}\n")

    print(f"Wrote {len(manifest)} entries -> {args.out_manifest}")
    print(f"Lock SHA-256: {lock}")
    print("Embed manifest in training job; use CDN URLs only at runtime.")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/discover_and_lock.py
```

Example usage (run on Mac orchestration host):
```bash
cd /opt/axentx/vanguard
python scripts/discover_and_lock.py \
  --repo datasets/myorg/vanguard-ingest \
  --date-folder batches/mirror-merged/2026-05-03 \
  --out-manifest manifests/2026-05-03.json \
  --out-lock locks/2026-05-03.sha256
```

Embed in training script (snippet):
```python
# train.py (Lightning job)
import json, requests, hashlib, os

MANIFEST_PATH = os.environ.get("MANIFEST_PATH", "manifests/2026-05-03.json")
with open(MANIFEST_PATH) as f:
    manifest = json.load(f)

def stream_cdn(entry):
    url = entry["cdn_url"]
    # CDN fetch — no Authorization header, bypasses HF API rate limits
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        yield from r.iter_content(chunk_size=8192)

# Use manifest entries deterministically (sorted by path already)
for entry in manifest:
    for chunk in stream_cdn(entry):
        # parse project-to-{prompt,response} here
        ...
```

## 4. Verification
1. Run discovery once:
   ```bash
   python scripts/discover_and_lock.py --repo datasets/myorg/vanguard-ingest --date-folder batches/mirror-merged/2026-05-03 --out-manifest /tmp/m.json --out-lock /tmp/l.sha256
   ```
   - Confirm `manifest.json` exists and is valid JSON.
   - Confirm `lock.sha256` contains a 64-char hex digest.

2. CDN reachability check (zero-auth):
   ```bash
   url=$(jq -r '.[0].cdn_url' /tmp/m.json)
   curl -fsI "$url" | head -1
   ```
   - Expect `HTTP/2 200`. No `WWW-Authenticate` required.

3. Determinism check:
   - Re-run discovery twice; `sha256sum /tmp/m.json` must match both times and match content of `/tmp/l.sha256`.

4. Training dry-run (local or Lightning quick test):
   - Set `MANIFEST_PATH=/tmp/m.json` and run a small parse pass over first two entries using CDN fetches only.
   - Monitor HF API dashboard or logs: zero `list_repo_tree` or `load_dataset` calls during data loading.

5. Lockfile gate in CI (optional):
   - Add step: `echo "<expected>  manifests/2026-05-03.json" | sha256sum -c -` before training start to enforce deterministic inputs.
