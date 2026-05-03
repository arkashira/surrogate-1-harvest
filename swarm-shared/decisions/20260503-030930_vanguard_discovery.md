# vanguard / discovery

# Final consolidated implementation

## Diagnosis (merged)
- No deterministic CDN-first manifest exists; training/ingestion scripts likely still call `list_repo_tree` or `load_dataset` at runtime, risking 429s and non-reproducible runs.
- Missing content-addressed file list with pinned ordering and integrity (SHA-256) for date-partitioned folders.
- No lightweight, Mac-safe CLI to produce the manifest on the orchestration host (single API call per folder) and embed it into training jobs.
- No verification step to ensure CDN URLs resolve and checksums match before training starts (wastes quota on corrupted/missing files).
- No reuse check for existing Running studios before launching new ones (wastes quota).
- No fallback behavior when Lightning Studio is stopped/idle (training dies on idle timeout).

## Chosen approach
- One deterministic CLI: `/opt/axentx/vanguard/bin/make-cdn-manifest` (single-file, Mac-safe, ~120 lines) that:
  - Accepts `REPO`, `FOLDER`, `OUT_JSON` env args (e.g., `datasets/surrogate-1/batches/mirror-merged/2026-04-29`)
  - Calls HF API **once** via `list_repo_tree` to list immediate files in the folder
  - Produces deterministic JSON sorted by filename with `path`, `cdn_url`, `size`, `sha256` (from ETag when available), and `etag`
  - Verifies every CDN URL resolves (HEAD) before writing the manifest; fails fast on unresolvable URLs
  - Writes `manifest.json` and a small `train_cdn.py` stub that uses only CDN URLs (zero API calls during training)
- One deterministic launcher: `/opt/axentx/vanguard/bin/launch-lightning-studio` that:
  - Reuses an existing Running studio by name if present
  - Starts a new one with `L40S` (free-tier fallback) if stopped/missing
  - Checks status before `.run()` and restarts if idle-killed
  - Retries with backoff and exits non-zero on unrecoverable failure

## Implementation

```bash
# Ensure directories exist
mkdir -p /opt/axentx/vanguard/bin
mkdir -p /opt/axentx/vanguard/templates
```

### /opt/axentx/vanguard/bin/make-cdn-manifest
```python
#!/usr/bin/env python3
"""
make-cdn-manifest
Deterministic CDN-first manifest generator for a Hugging Face dataset folder.

Usage (Mac orchestration):
  HF_TOKEN=<token> \
  REPO=datasets/surrogate-1 \
  FOLDER=batches/mirror-merged/2026-04-29 \
  OUT_JSON=manifest.json \
  /opt/axentx/vanguard/bin/make-cdn-manifest

Outputs:
  - manifest.json (deterministic, sorted, content-addressed)
  - train_cdn.py   (CDN-only training stub)
"""
import os
import sys
import json
import time
import hashlib
import requests
from typing import List, Dict, Optional

HF_API = "https://huggingface.co/api"
HF_CDN = "https://huggingface.co/datasets"

# Deterministic sort key for stable manifests
def _sort_key(entry: Dict) -> str:
    return entry["path"]

def list_folder_files(repo: str, folder: str, token: Optional[str]) -> List[str]:
    """Single API call: list immediate files in folder (non-recursive)."""
    url = f"{HF_API}/{repo}/tree/{folder}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 404:
        # try root listing if folder empty
        resp = requests.get(f"{HF_API}/{repo}/tree", headers=headers, timeout=30)
    resp.raise_for_status()
    items = resp.json()
    # keep only files directly under folder (no subfolders)
    prefix = folder.rstrip("/") + "/"
    files = [
        item["path"]
        for item in items
        if item.get("type") == "file" and (
            item.get("path", "") == folder.rstrip("/") or item.get("path", "").startswith(prefix)
        )
    ]
    return sorted(set(files))

def head_cdn(repo: str, filepath: str) -> Dict:
    """HEAD CDN URL; return size, etag, and sha256 if available."""
    url = f"{HF_CDN}/{repo}/resolve/main/{filepath}"
    resp = requests.head(url, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    size = int(resp.headers.get("Content-Length", -1))
    etag = resp.headers.get("ETag", "")
    sha256 = None
    if etag:
        etag = etag.strip().strip('W/"').strip('"')
        if len(etag) == 64 and all(c in "0123456789abcdef" for c in etag.lower()):
            sha256 = etag.lower()
    return {"cdn_url": url, "size": size, "etag": etag, "sha256": sha256}

def verify_cdn(repo: str, filepath: str) -> bool:
    """Verify CDN URL resolves (HEAD). Returns True if OK."""
    url = f"{HF_CDN}/{repo}/resolve/main/{filepath}"
    try:
        resp = requests.head(url, allow_redirects=True, timeout=30)
        return resp.status_code == 200
    except Exception:
        return False

def build_manifest(repo: str, folder: str, token: Optional[str], verify: bool = True) -> Dict:
    files = list_folder_files(repo, folder, token)
    entries = []
    for f in files:
        if verify:
            ok = verify_cdn(repo, f)
            if not ok:
                raise RuntimeError(f"CDN verification failed for {f}")
        meta = head_cdn(repo, f)
        entries.append(
            {
                "path": f,
                "cdn_url": meta["cdn_url"],
                "size": meta["size"],
                "sha256": meta["sha256"],
                "etag": meta["etag"],
            }
        )
    manifest = {
        "repo": repo,
        "folder": folder.rstrip("/"),
        "generated_by": "make-cdn-manifest",
        "count": len(entries),
        "entries": sorted(entries, key=_sort_key),
    }
    return manifest

def write_stub(manifest_file: str) -> str:
    stub = '''#!/usr/bin/env python3
"""
train_cdn.py — CDN-only training stub.
Uses manifest produced by make-cdn-manifest.
During training, only CDN URLs are used — zero HF API calls.
"""
import json
import os
from pathlib import Path

MANIFEST_PATH = Path("{manifest_file}").expanduser().resolve()
with open(MANIFEST_PATH) as f:
    MANIFEST = json.load(f)

CDN_URLS = [e["cdn_url"] for e in MANIFEST["entries"]]

def get_cdn_urls():
    return CDN_URLS

if __name__ == "__main__":
    print(f"Loaded {{len(CDN_URLS)}} CDN URLs from {{MANIFEST_PATH.name}}")
    # Replace dataset loading in your training script with CDN_URLS.
    # Example: use requests or datasets with data_files=CDN_URLS
'''.format(manifest_file=manifest_file)
    stub_path = "train_cdn.py"
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(stub)
    return stub_path

def main() -> None:
    repo = os.getenv("REPO")
    folder = os.getenv("FOLDER", "")
    out_json = os.getenv("OUT_JSON", "manifest.json")
    token = os.getenv("HF_TOKEN", "")
    verify = os.getenv("VERIFY", "1") == "1"

    if not repo:
        print("ERROR: set REPO env (e.g., datasets/surrogate-1)", file=sys.stderr)
        sys.exit(1)

    print(f"Listing {repo}/{folder or '(root)'} ...")
    manifest = build_manifest(repo, folder
