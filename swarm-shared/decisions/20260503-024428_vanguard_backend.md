# vanguard / backend

### Final Synthesis (Best Parts + Correctness + Actionable)

**Diagnosis (resolved)**
- Runtime HF API calls from backend/frontend burn quota and cause 429s.  
- No build-time or embedded static file manifest → every session re-enumerates repos.  
- No CDN bypass for dataset files → unnecessary auth/API load.  
- No retry/backoff for transient HF API failures.  
- No mitigation for HF commit caps or idle-stop–induced training loss (Lightning Studio).  

**Proposed change (single scope)**
Add a build-time manifest generator, a runtime CDN-only data provider with retries, and deterministic repo selection to bypass rate/ commit limits. All changes in `vanguard/backend` plus one build script; <200 lines.

---

### Implementation (concrete, minimal, correct)

#### 1) Create directory layout
```bash
cd /opt/axentx/vanguard
mkdir -p scripts data/manifests vanguard/backend
touch vanguard/backend/__init__.py
```

#### 2) Build-time manifest generator  
`scripts/build_file_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate file_manifest_{date}.json for a given date-folder repo.
Run during CI/build or once per dataset version.
"""
import json
import os
import sys
from datetime import datetime

# Import your actual HF client wrapper (adjust import path as needed)
try:
    from vanguard.backend.api_client import list_repo_tree
except Exception:
    # Fallback stub for standalone testing
    def list_repo_tree(repo_id, path="", recursive=True):
        # Replace with real HF API call (huggingface_hub or requests)
        raise NotImplementedError("Provide real list_repo_tree impl")

DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
REPO_ID = os.getenv("HF_DATASET_REPO", f"datasets/myorg/mydata-{DATE_FOLDER}")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "manifests")
os.makedirs(OUT_DIR, exist_ok=True)
OUT_FILE = os.path.join(OUT_DIR, f"file_manifest_{DATE_FOLDER}.json")

def build_manifest():
    entries = []
    try:
        tree = list_repo_tree(REPO_ID, path="", recursive=True)
    except Exception as e:
        print(f"Failed to list repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    for node in tree:
        if node.get("type") == "file":
            entries.append(node["path"])

    manifest = {
        "repo": REPO_ID,
        "date": DATE_FOLDER,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": sorted(set(entries))
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {OUT_FILE} ({len(entries)} files)")

if __name__ == "__main__":
    build_manifest()
```

#### 3) Runtime CDN-only data provider with retries  
`vanguard/backend/data_provider.py`
```python
import json
import os
import random
import time
from typing import List, Dict, Optional
import requests

MANIFEST_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "manifests")

def _retry_with_backoff(func, max_retries: int = 5, backoff_factor: float = 1.5):
    def wrapper(*args, **kwargs):
        retries = 0
        while True:
            try:
                return func(*args, **kwargs)
            except (requests.HTTPError, requests.ConnectionError, TimeoutError) as exc:
                if retries >= max_retries:
                    raise
                wait = (backoff_factor ** retries) + random.uniform(0, 1)
                time.sleep(wait)
                retries += 1
    return wrapper

@_retry_with_backoff
def _http_get(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

class CDNDataProvider:
    """
    Reads embedded manifest and serves dataset file contents via CDN URLs
    without HF API calls.
    """
    def __init__(self, date_folder: str, repo: Optional[str] = None):
        self.date_folder = date_folder
        manifest_path = os.path.join(MANIFEST_DIR, f"file_manifest_{date_folder}.json")
        if not os.path.isfile(manifest_path):
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")

        with open(manifest_path, "r", encoding="utf-8") as f:
            self.manifest = json.load(f)

        self.repo = repo or self.manifest.get("repo")
        if not self.repo:
            raise ValueError("Repo not specified and not found in manifest")
        self._files = set(self.manifest.get("files", []))

    def list_files(self) -> List[str]:
        return sorted(self._files)

    def get_file_cdn_url(self, path: str) -> str:
        # Prefer datasets/ repo style; fallback to generic resolve
        if self.repo.startswith("datasets/"):
            repo_part = self.repo
        else:
            repo_part = f"datasets/{self.repo}"
        return f"https://huggingface.co/{repo_part}/resolve/main/{path}"

    def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(f"File not in manifest: {path}")
        url = self.get_file_cdn_url(path)
        return _http_get(url)

    def read_files(self, paths: Optional[List[str]] = None) -> Dict[str, bytes]:
        paths = paths or self.list_files()
        return {p: self.read_file(p) for p in paths}
```

#### 4) Deterministic repo selector for commit-cap mitigation  
`vanguard/backend/hf_repos.py`
```python
import hashlib

SIBLING_REPOS = [
    "datasets/myorg/mydata-shard-a",
    "datasets/myorg/mydata-shard-b",
    "datasets/myorg/mydata-shard-c",
    "datasets/myorg/mydata-shard-d",
    "datasets/myorg/mydata-shard-e",
]

def select_repo(key: str, repos=None) -> str:
    """
    Deterministically pick a sibling repo by hashing key.
    Use to spread commits and bypass per-repo commit caps.
    """
    repos = repos or SIBLING_REPOS
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    idx = int(digest, 16) % len(repos)
    return repos[idx]
```

#### 5) Small edit: wire provider into existing entrypoint  
Example patch to your backend loader (adjust to your real file):
```python
# In your existing backend entrypoint/module:
try:
    from vanguard.backend.data_provider import CDNDataProvider
    DATA_PROVIDER = CDNDataProvider(date_folder="2024-01-01")  # or derive from config/env
except Exception:
    DATA_PROVIDER = None
```

---

### Verification (actionable checklist)

1. **Build manifest**  
   ```bash
   DATE_FOLDER=2024-01-01 HF_DATASET_REPO=datasets/myorg/mydata-2024-01-01 \
     python scripts/build_file_manifest.py
   ```
   Confirm `vanguard/data/manifests/file_manifest_2024-01-01.json` exists and lists files.

2. **CDN-only reads**  
   In a Python shell:
   ```python
   from vanguard.backend.data_provider import CDNDataProvider
   p = CDNDataProvider("2024-01-01")
   print(p.list_files()[:5])
   sample = p.read_file(p.list_files()[0])
   assert len(sample) > 0
   ```
   Verify no HF API calls occur (check logs / `list_repo_tree` not invoked).

3. **Retry/backoff**  
   Temporarily force 5xx/429 from CDN or use a mock; confirm exponential backoff retries and eventual success/raise.

4. **Repo selector**  
   ```python
   from v
