# vanguard / discovery

## Final Synthesized Implementation

### 1. Diagnosis (Consolidated)
- **No CDN-first manifest**: training/ingestion scripts still rely on HF API (`list_repo_tree`/`load_dataset`) and risk 429s.
- **No integrity verification**: silent corruption possible during long surrogate-1 training runs.
- **No reproducible slice selection**: training jobs cannot pin exact file set and ordering, causing non-deterministic epochs across restarts.
- **No Mac-side orchestration stub**: no lightweight pre-listing step to embed paths/hashes so Lightning Studio does zero API calls during data load.
- **Missing deterministic verification**: no lightweight step to confirm CDN downloads match HF repo state before training starts.

### 2. Proposed Change
Create `/opt/axentx/vanguard/scripts/build_manifest.py` (single file) plus a companion patch to `/opt/axentx/vanguard/train.py` that consumes the manifest and downloads exclusively via CDN.  
Scope: one new script (~120–150 lines), one small edit to `train.py`, no new runtime dependencies beyond `requests`, `pyarrow`, and standard libs.

### 3. Implementation

#### 3.1 Create `/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
build_manifest.py
Usage (Mac orchestrator):
  HF_REPO="datasets/axentx/surrogate-1" \
  HF_TOKEN="hf_xxx" \
  FOLDER="batches/mirror-merged/2026-05-03" \
  OUT="manifests/vanguard-2026-05-03.json" \
  python scripts/build_manifest.py

Produces a deterministic manifest with CDN URLs and sha256 hashes.
"""
import os
import sys
import json
import hashlib
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

HF_API = "https://huggingface.co/api"
CDN_ROOT = "https://huggingface.co/datasets"
DEFAULT_RETRY = 3
BACKOFF = 5

def _headers() -> Dict[str, str]:
    token = os.getenv("HF_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}

def list_folder_files(repo: str, folder: str) -> List[str]:
    """
    Single non-recursive API call to list files in one folder.
    repo format: datasets/owner/name
    """
    repo = repo.removeprefix("datasets/")
    url = f"{HF_API}/datasets/{repo}/tree"
    params = {"path": folder, "recursive": "false"}
    for attempt in range(1, DEFAULT_RETRY + 1):
        try:
            r = requests.get(url, headers=_headers(), params=params, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", BACKOFF * attempt))
                print(f"Rate-limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            items = r.json()
            paths = [
                item["path"]
                for item in items
                if item.get("type") == "file" and item["path"].endswith(".parquet")
            ]
            return sorted(paths)
        except Exception:
            if attempt == DEFAULT_RETRY:
                raise
            time.sleep(BACKOFF * attempt)
    return []

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def cdn_url(repo: str, path: str) -> str:
    repo = repo.removeprefix("datasets/")
    return f"{CDN_ROOT}/{repo}/resolve/main/{path}"

def fetch_with_retry(url: str) -> bytes:
    for attempt in range(1, DEFAULT_RETRY + 1):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", BACKOFF * attempt))
                print(f"CDN rate-limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.content
        except Exception:
            if attempt == DEFAULT_RETRY:
                raise
            time.sleep(BACKOFF * attempt)
    raise RuntimeError(f"Failed to fetch {url}")

def build_manifest(repo: str, folder: str, max_workers: int = 8) -> Dict:
    files = list_folder_files(repo, folder)
    print(f"Found {len(files)} parquet files in {folder}", file=sys.stderr)

    manifest = {
        "repo": repo,
        "folder": folder,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": [],
    }

    def process(path: str) -> Dict:
        url = cdn_url(repo, path)
        data = fetch_with_retry(url)
        return {
            "path": path,
            "url": url,
            "size": len(data),
            "sha256": sha256_bytes(data),
        }

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(process, p): p for p in files}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                entry = fut.result()
                manifest["files"].append(entry)
                print(f"OK  {p}  sha256:{entry['sha256'][:12]}")
            except Exception as e:
                print(f"FAIL {p}  {e}", file=sys.stderr)
                raise

    manifest["files"].sort(key=lambda x: x["path"])
    return manifest

def main() -> None:
    repo = os.getenv("HF_REPO") or (sys.argv[1] if len(sys.argv) > 1 else None)
    folder = os.getenv("FOLDER") or (sys.argv[2] if len(sys.argv) > 2 else None)
    out_path = os.getenv("OUT") or (sys.argv[3] if len(sys.argv) > 3 else None)

    if not repo or not folder or not out_path:
        print(
            "Usage: HF_REPO=datasets/owner/name FOLDER=path OUT=file.json python build_manifest.py",
            file=sys.stderr,
        )
        sys.exit(1)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    manifest = build_manifest(repo, folder)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"Manifest written to {out_path}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

#### 3.2 Patch `/opt/axentx/vanguard/train.py` to consume manifest and use CDN-only downloads
Add/replace the data-loading section in `/opt/axentx/vanguard/train.py`:

```python
import json
import os
import hashlib
import requests
import pyarrow.parquet as pq
from io import BytesIO
from typing import List, Dict, Any

def load_manifest(manifest_path: str) -> Dict[str, Any]:
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)

def verify_and_load_parquet_from_cdn(entry: Dict[str, Any]) -> pq.Table:
    """
    Download via CDN and verify sha256 before parsing.
    """
    url = entry["url"]
    expected = entry["sha256"]
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.content
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Hash mismatch for {url}: expected {expected}, got {actual}")
    return pq.read_table(BytesIO(data))

def build_dataset_from_manifest(
    manifest_path: str,
    prompt_col: str = "prompt",
    response_col: str = "response",
) -> List[Dict[str, str]]:
    """
    Determ
