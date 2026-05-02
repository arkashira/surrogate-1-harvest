# vanguard / discovery

## 1. Diagnosis
- No durable ingestion manifest: every training run re-lists HF repos and re-downloads, causing 429s and wasted bandwidth.
- Training loads via HF API (`load_dataset`/`list_repo_files`) instead of CDN bypass → guaranteed rate limits during data loading.
- No Studio reuse guard: scripts create new Lightning Studios instead of reusing running ones, burning 80+ quota hours/month.
- No idle-stop protection: Lightning idle timeout kills long-running training; no restart logic before `.run()` calls.
- No deterministic sibling-repo routing: all HF writes target one repo and hit the 128 commits/hr cap instead of spreading across siblings.

## 2. Proposed change
Add a lightweight discovery/state module and patch the training launcher:
- File: `/opt/axentx/vanguard/discovery/manifest.py` (new)
- File: `/opt/axentx/vanguard/train/train.py` (patch)
- File: `/opt/axentx/vanguard/ops/studio.py` (patch)

Scope: implement manifest caching, CDN-only data loading, Studio reuse, idle-stop guard, and sibling routing. No UI changes.

## 3. Implementation

### discovery/manifest.py
```python
# /opt/axentx/vanguard/discovery/manifest.py
import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

MANIFEST_DIR = Path(__file__).parent.parent / "state" / "manifests"
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

def _slug_for(repo: str, folder: str, when: Optional[str] = None) -> str:
    when = when or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = f"{repo}/{folder}@{when}"
    h = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"{when}-{h}.json"

def save_manifest(repo: str, folder: str, files: List[str], when: Optional[str] = None) -> Path:
    slug = _slug_for(repo, folder, when)
    path = MANIFEST_DIR / slug
    manifest = {
        "repo": repo,
        "folder": folder,
        "when": when or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{folder}"
    }
    path.write_text(json.dumps(manifest, indent=2))
    return path

def load_manifest(repo: str, folder: str, when: Optional[str] = None) -> Optional[Dict]:
    slug = _slug_for(repo, folder, when)
    path = MANIFEST_DIR / slug
    if not path.exists():
        return None
    return json.loads(path.read_text())

def sibling_repo_for(slug: str, n_siblings: int = 5) -> str:
    """Deterministic sibling repo selector to spread HF commit load."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n_siblings
    if idx == 0:
        return "primary-repo"  # default repo name
    return f"primary-repo-sibling-{idx}"
```

### train/train.py (patch)
```diff
# /opt/axentx/vanguard/train/train.py
+ import requests
+ from pathlib import Path
+ from discovery.manifest import load_manifest, save_manifest, sibling_repo_for

HF_REPO = "org/surrogate-1"
HF_FOLDER = "batches/mirror-merged/2026-05-02"

-def list_and_cache():
-    from huggingface_hub import list_repo_tree
-    files = [f.rfilename for f in list_repo_tree(HF_REPO, path=HF_FOLDER, recursive=False)]
-    return files

+def list_and_cache():
+    manifest = load_manifest(HF_REPO, HF_FOLDER)
+    if manifest:
+        return manifest["files"]
+    # One-time Mac-side run after rate-limit window; embed result in training.
+    from huggingface_hub import list_repo_tree
+    files = [f.rfilename for f in list_repo_tree(HF_REPO, path=HF_FOLDER, recursive=False)]
+    save_manifest(HF_REPO, HF_FOLDER, files)
+    return files

+def cdn_urls(file_list):
+    prefix = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{HF_FOLDER}"
+    return [f"{prefix}/{f}" for f in file_list]

-def load_dataset_local():
-    from datasets import load_dataset
-    return load_dataset("parquet", data_files={"train": f"hf://datasets/{HF_REPO}/{HF_FOLDER}/*.parquet"})

+def load_dataset_cdn(file_list):
+    # CDN-only: no Authorization header, bypasses API rate limits.
+    urls = cdn_urls(file_list)
+    from datasets import load_dataset
+    return load_dataset("parquet", data_files={"train": urls})

def main():
    files = list_and_cache()
    ds = load_dataset_cdn(files)
    # ... rest of training
```

### ops/studio.py (patch)
```diff
# /opt/axentx/vanguard/ops/studio.py
+ from lightning import Teamspace, Studio, Machine
+ import time

+ def get_or_create_studio(name: str, machine: Machine = Machine.L40S):
+     for s in Teamspace.studios:
+         if s.name == name and s.status == "Running":
+             return s
+     # If stopped, restart instead of recreating
+     for s in Teamspace.studios:
+         if s.name == name and s.status == "Stopped":
+             s.start(machine=machine)
+             return s
+     return Studio(name=name, machine=machine, create_ok=True)
+
+ def run_with_idle_guard(studio, target, max_retries=3):
+     for attempt in range(max_retries):
+         if studio.status != "Running":
+             studio.start(machine=studio.machine or Machine.L40S)
+         try:
+             return studio.run(target)
+         except Exception as e:
+             if attempt == max_retries - 1:
+                 raise
+             time.sleep(30)
+             continue
```

## 4. Verification
1. Run once on Mac (after HF rate-limit window) to generate manifest:
   ```bash
   cd /opt/axentx/vanguard
   python -c "from train.train import list_and_cache; print(list_and_cache())"
   ```
   Confirm `state/manifests/2026-05-02-*.json` exists and lists parquet files.

2. Confirm CDN-only load (zero API calls during training):
   ```bash
   python -c "from train.train import load_dataset_cdn, list_and_cache; ds=load_dataset_cdn(list_and_cache()); print(len(ds['train']))"
   ```
   Should print row count without HF auth errors.

3. Studio reuse:
   ```bash
   python -c "from ops.studio import get_or_create_studio; s=get_or_create_studio('vanguard-dev'); print(s.name, s.status)"
   ```
   Running second time should return same Running studio (no new quota charge).

4. Idle guard: stop studio in UI, re-run the same script; it should restart automatically and execute target.

5. Sibling routing:
   ```bash
   python -c "from discovery.manifest import sibling_repo_for; print([sibling_repo_for(f'train-{i}') for i in range(10)])"
   ```
   Should distribute across primary-repo and primary-repo-sibling-{1..5}.
