# vanguard / discovery

## Final Synthesized Implementation

### Diagnosis (merged)
- No content-addressed manifest per date folder forces runtime repo enumeration via `list_repo_tree`/`load_dataset`, triggering HF API 429s and non-reproducible epochs.
- Missing deterministic `{path, sha256}` snapshot means CDN-only fetches cannot be validated or resumed reliably; corrupted/partial downloads silently poison training.
- Training/ingestion scripts likely re-list folders or call HF API during data loading instead of using a precomputed file list + CDN-only fetches, wasting quota and increasing flakiness.
- No lightweight validation layer to ensure local cache matches remote content before training starts (hash check per shard).
- No reusable script to snapshot a date folder into a manifest that can be committed and reused across runs (Mac orchestration → Lightning execution).

### Proposed change (merged)
Create two small, reusable Python modules:
1. **Manifest generator** (`make_manifest.py`):  
   - Accepts `HF_REPO` and `DATE_FOLDER` (e.g. `batches/mirror-merged/2026-05-03`)  
   - Uses **one** non-recursive `list_repo_tree` call to list immediate parquet files in that folder  
   - Computes SHA256 via streaming CDN fetch (no full buffering) and records `{path, sha256, size}`  
   - Emits `manifests/{DATE_FOLDER_SLUG}.jsonl` and a companion `train_filelist_{DATE_FOLDER_SLUG}.txt` of CDN URLs  

2. **Validation + training helper** (`validate_and_train.py`):  
   - Loads the manifest  
   - Verifies local cache (or re-downloads via CDN) with hash check  
   - Produces a `DataLoader`-friendly iterable of `{path, local_path|url, sha256}` for Lightning training  

### Implementation (merged + corrected)

```bash
# Create project structure
mkdir -p /opt/axentx/vanguard/{scripts,manifests,cache}
chmod +x /opt/axentx/vanguard/scripts/*.py
```

```python
# /opt/axentx/vanguard/scripts/make_manifest.py
#!/usr/bin/env python3
"""
Create content-addressed manifest for a date folder in an HF dataset repo.
Usage:
  HF_REPO=datasets/username/repo DATE=batches/mirror-merged/2026-05-03 \
    python make_manifest.py
Outputs:
  manifests/{DATE_FOLDER_SLUG}.jsonl
  train_filelist_{DATE_FOLDER_SLUG}.txt  (CDN URLs)
"""
import os
import sys
import json
import hashlib
import requests
from pathlib import Path

HF_API = "https://huggingface.co/api"
HF_CDN = "https://huggingface.co/datasets"

def list_files(repo: str, folder: str):
    """Single API call: non-recursive tree listing for folder."""
    url = f"{HF_API}/datasets/{repo}/tree"
    params = {"path": folder, "recursive": False}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    items = resp.json()
    # Keep only parquet files in this folder (not subfolders)
    return [i for i in items if i.get("type") == "file" and i["path"].endswith(".parquet")]

def sha256_cdn_stream(repo: str, path: str):
    """Stream from CDN and compute sha256 without full buffering."""
    url = f"{HF_CDN}/{repo}/resolve/main/{path}"
    h = hashlib.sha256()
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=8192):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(repo: str, folder: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = folder.strip("/").replace("/", "_") or "root"
    manifest_path = out_dir / f"{slug}.jsonl"
    filelist_path = out_dir / f"train_filelist_{slug}.txt"

    entries = []
    cdn_urls = []
    files = list_files(repo, folder)
    if not files:
        print(f"No parquet files found in {repo}/{folder}", file=sys.stderr)
        return

    for f in files:
        path = f["path"]
        size = f.get("size", 0)
        print(f"Hashing {path}...", file=sys.stderr)
        sha = sha256_cdn_stream(repo, path)
        entry = {"path": path, "sha256": sha, "size": size}
        entries.append(entry)
        cdn_urls.append(f"{HF_CDN}/{repo}/resolve/main/{path}")

    with manifest_path.open("w") as mf:
        for e in entries:
            mf.write(json.dumps(e) + "\n")

    with filelist_path.open("w") as fl:
        fl.write("\n".join(cdn_urls) + "\n")

    print(f"Manifest: {manifest_path}")
    print(f"Filelist: {filelist_path}")

if __name__ == "__main__":
    repo = os.getenv("HF_REPO")
    folder = os.getenv("DATE")
    if not repo or not folder:
        print("Set HF_REPO and DATE env vars", file=sys.stderr)
        sys.exit(1)
    build_manifest(repo, folder, Path(__file__).parent.parent / "manifests")
```

```python
# /opt/axentx/vanguard/scripts/validate_and_train.py
#!/usr/bin/env python3
"""
Validate cache against manifest and produce CDN-based dataset iterable.
Lightning training script should import get_cdn_dataset(manifest_path).
"""
import json
import hashlib
import requests
from pathlib import Path
from typing import Iterator, Dict

HF_CDN = "https://huggingface.co/datasets"
CACHE_ROOT = Path(__file__).parent.parent / "cache"

def load_manifest(manifest_path: str) -> list[Dict]:
    with open(manifest_path) as f:
        return [json.loads(l) for l in f]

def cached_path(repo: str, path: str) -> Path:
    # deterministic cache path
    safe = path.replace("/", "_")
    return CACHE_ROOT / repo.replace("/", "_") / safe

def ensure_cached(repo: str, path: str, expected_sha: str) -> Path:
    cp = cached_path(repo, path)
    if cp.exists():
        h = hashlib.sha256(cp.read_bytes()).hexdigest()
        if h == expected_sha:
            return cp
        cp.unlink()  # corrupted
    url = f"{HF_CDN}/{repo}/resolve/main/{path}"
    cp.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(cp, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    h = hashlib.sha256(cp.read_bytes()).hexdigest()
    if h != expected_sha:
        cp.unlink()
        raise ValueError(f"Hash mismatch for {path}")
    return cp

def get_cdn_dataset(repo: str, manifest_path: str, use_cache: bool = True) -> Iterator[Dict]:
    """Yield {path, local_path|url, sha256} for training consumption."""
    entries = load_manifest(manifest_path)
    for e in entries:
        path, sha = e["path"], e["sha256"]
        if use_cache:
            local = ensure_cached(repo, path, sha)
            yield {"path": path, "local_path": str(local), "sha256": sha}
        else:
            url = f"{HF_CDN}/{repo}/resolve/main/{path}"
            yield {"path": path, "url": url, "sha256": sha}

# Example Lightning usage (pseudo):
# dataset = get_cdn_dataset(repo, "manifests/batches_mirror-merged_2026-05-03.jsonl", use_cache=False)
# Then map CDN URLs into your HF dataset loader or custom IterableDataset.
```

### Key correctness + actionability choices (
