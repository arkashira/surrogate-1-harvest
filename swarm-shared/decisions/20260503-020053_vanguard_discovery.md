# vanguard / discovery

# 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` manifest: every training/data-selection run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Data loader likely uses `load_dataset(streaming=True)` or repeated per-file API calls on heterogeneous repos, causing `pyarrow.CastError` from mixed schemas.
- Training does not use HF CDN bypass: authenticated API calls during data loading instead of zero-auth CDN fetches, increasing rate-limit pressure.
- No reuse strategy for Lightning Studio: training scripts likely create new studios instead of reusing running ones, wasting 80+ hrs/mo of quota.
- No deterministic shard-to-repo mapping for HF ingestion: writes likely target a single repo, risking the 128 commits/hr cap and blocking ingestion.

# 2. Proposed change

Create `/opt/axentx/vanguard/discovery/persist_filelist.py` (single-purpose, <150 LOC) that:
- Accepts `repo` and `dateFolder` on CLI.
- Calls `list_repo_tree(repo, path=dateFolder, recursive=False)` **once** (Mac-side, after rate-limit window).
- Persists `{repo}/{dateFolder}/filelist.json` to `/opt/axentx/vanguard/data/manifests/`.
- Emits a companion `train_cdn_only.py` stub that loads the manifest and uses only CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero HF API auth during training.

# 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/discovery /opt/axentx/vanguard/data/manifests
```

```python
# /opt/axentx/vanguard/discovery/persist_filelist.py
#!/usr/bin/env python3
"""
Persist file manifest for a repo/dateFolder to avoid repeated HF API list calls.
Usage:
  python persist_filelist.py huggingface.co/datasets/owner/repo 2026-04-29
"""
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

REPO_ROOT = Path("/opt/axentx/vanguard")
MANIFEST_DIR = REPO_ROOT / "data/manifests"

def normalize_repo(repo: str) -> str:
    # Accept huggingface.co/datasets/owner/repo or owner/repo
    p = repo.strip("/")
    if p.startswith("huggingface.co/datasets/"):
        p = p.replace("huggingface.co/datasets/", "")
    return p

def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: persist_filelist.py <repo> <dateFolder>")
        sys.exit(1)

    repo = normalize_repo(sys.argv[1])
    date_folder = sys.argv[2].strip("/")

    print(f"Listing {repo} @ {date_folder} ...")
    try:
        items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    except Exception as e:
        print(f"HF API error: {e}")
        sys.exit(1)

    files = [it.rfilename for it in items if it.type == "file"]
    manifest = {
        "repo": repo,
        "dateFolder": date_folder,
        "files": sorted(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}"
    }

    out_dir = MANIFEST_DIR / repo / date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "filelist.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Persisted {len(files)} files -> {out_path}")

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/discovery/train_cdn_only.py
#!/usr/bin/env python3
"""
Lightning training stub that uses CDN-only fetches (zero HF API auth during training).
Embed the manifest produced by persist_filelist.py.
"""
import json
from pathlib import Path
from typing import List, Dict
import requests

MANIFEST_PATH = Path(__file__).parent.parent / "data/manifests"

def load_manifest(repo: str, date_folder: str) -> Dict:
    p = MANIFEST_PATH / repo / date_folder / "filelist.json"
    if not p.exists():
        raise FileNotFoundError(f"Manifest missing: {p}")
    return json.loads(p.read_text())

def cdn_fetch_bytes(cdn_url: str, timeout: int = 30) -> bytes:
    # CDN fetch: no Authorization header required
    resp = requests.get(cdn_url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def build_cdn_urls(manifest: Dict) -> List[str]:
    prefix = manifest["cdn_prefix"].rstrip("/")
    return [f"{prefix}/{f}" for f in manifest["files"]]

# Example usage inside Lightning training script:
#   manifest = load_manifest("owner/repo", "2026-04-29")
#   urls = build_cdn_urls(manifest)
#   for u in urls:
#       data = cdn_fetch_bytes(u)
#       ... project to {prompt, response} and train ...
```

```bash
# Make scripts executable
chmod +x /opt/axentx/vanguard/discovery/persist_filelist.py
chmod +x /opt/axentx/vanguard/discovery/train_cdn_only.py
```

# 4. Verification

1. Run manifest creation (from Mac, after HF rate-limit window is clear):
   ```bash
   cd /opt/axentx/vanguard
   python discovery/persist_filelist.py huggingface.co/datasets/owner/repo 2026-04-29
   ```
   - Expect: `Persisted N files -> data/manifests/owner/repo/2026-04-29/filelist.json`
   - Confirm JSON contains `repo`, `dateFolder`, `files[]`, `cdn_prefix`.

2. Validate CDN URLs resolve without auth:
   ```bash
   head -1 data/manifests/owner/repo/2026-04-29/filelist.json | jq -r '.cdn_prefix + "/" + .files[0]' | xargs curl -I
   ```
   - Expect: `HTTP/2 200` (or 302/200 redirect). No 401/403/429 from API.

3. Smoke-test training stub (dry-run, no GPU):
   ```bash
   python discovery/train_cdn_only.py 2>&1 | head -20
   ```
   - Expect: no import errors; helper functions load manifest and build URLs.

4. Confirm quota savings:
   - Before: each training run triggered multiple `list_repo_tree`/`load_dataset` API calls.
   - After: only one authenticated API call per `(repo,dateFolder)` (when manifest is created); training uses CDN-only fetches with zero auth headers.
