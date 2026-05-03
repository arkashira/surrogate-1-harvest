# vanguard / discovery

## Final Synthesis & Action Plan

**Chosen architecture:**  
A single, deterministic manifest layer that **eliminates authenticated HF API calls during training**, plus **Lightning Studio reuse with idempotent lifecycle management**.

---

### 1. Unified Diagnosis (accepted from both)
- **Problem:** Every training run re-enumerates HF repos via authenticated API → quota burn, 429 risk, and PyArrow schema churn on heterogeneous repos.
- **Amplifier:** Recursive or per-batch HF fetches during training instead of CDN-only reads.
- **Orchestration waste:** No deterministic Lightning Studio reuse → duplicate sessions, quota churn, scheduling instability.
- **Missing guardrails:** No deterministic repo selection or commit-cap awareness (128/hr/repo) for large mirrors.

---

### 2. Unified Solution (best parts merged)

**Core invariant:**  
After manifest generation, **training uses only public CDN URLs and never calls `huggingface_hub` APIs**.

**File layout (final):**
```
/opt/axentx/vanguard/
├─ manifests/
│  └─ {repo_safe}-{date_folder}-{hash}.json
├─ repo_index.json            # deterministic repo assignment + commit-cap ledger
├─ manifest.py                # generation + CDN helpers
├─ train.py                   # loader using CDN-only URLs
└─ launch_studio.py           # idempotent reuse + idle guard
```

---

### 3. Implementation (merged + hardened)

```python
# /opt/axentx/vanguard/manifest.py
#!/usr/bin/env python3
"""
Generate and reuse CDN-only manifests for HF datasets.
Avoids authenticated API calls during training.
"""
import json, os, hashlib, datetime, time
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree, HfApi
except ImportError:
    import subprocess
    subprocess.run(["pip", "install", "huggingface_hub"], check=True)
    from huggingface_hub import list_repo_tree, HfApi

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

# Deterministic repo index for commit-cap fairness (128 commits/hr/repo)
REPO_INDEX_PATH = Path(__file__).parent / "repo_index.json"

def _load_repo_index() -> Dict:
    if REPO_INDEX_PATH.exists():
        return json.loads(REPO_INDEX_PATH.read_text())
    return {"repos": [], "last_assigned": {}}

def _save_repo_index(idx: Dict) -> None:
    REPO_INDEX_PATH.write_text(json.dumps(idx, indent=2))

def assign_repo_slot(repo: str, date_folder: str) -> str:
    """Deterministic slot assignment to serialize large mirror ingestion."""
    idx = _load_repo_index()
    key = f"{repo}::{date_folder}"
    if key not in idx["last_assigned"]:
        slot = len(idx["repos"])
        idx["repos"].append({"key": key, "repo": repo, "date_folder": date_folder})
        idx["last_assigned"][key] = {
            "slot": slot,
            "assigned_at": datetime.datetime.utcnow().isoformat() + "Z"
        }
        _save_repo_index(idx)
    return idx["last_assigned"][key]["slot"]

def slug_for(repo: str, date_folder: str) -> str:
    h = hashlib.sha256(f"{repo}::{date_folder}".encode()).hexdigest()[:12]
    safe_repo = repo.replace("/", "_")
    return f"{safe_repo}-{date_folder}-{h}.json"

def build_manifest(
    repo: str,
    date_folder: str,
    overwrite: bool = False,
    page_size: int = 1000,
    delay: float = 0.5
) -> Path:
    """
    Non-recursive, paginated tree listing for a date folder.
    Returns path to JSON manifest with CDN paths only.
    """
    out_path = MANIFEST_DIR / slug_for(repo, date_folder)
    if out_path.exists() and not overwrite:
        return out_path

    api = HfApi()
    files: List[Dict] = []
    cursor = None

    while True:
        # Paginated non-recursive listing
        tree = api.list_repo_tree(
            repo=repo,
            path=date_folder,
            recursive=False,
            cursor=cursor,
            limit=page_size
        )
        page_files = [
            {"path": f"{date_folder}/{node.path.split('/')[-1]}"}
            for node in tree if node.type == "file"
        ]
        files.extend(page_files)

        cursor = getattr(tree, "cursor", None)
        if not cursor:
            break
        time.sleep(delay)  # gentle pacing

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "slot": assign_repo_slot(repo, date_folder),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "files": files
    }

    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path

def cdn_url(repo: str, file_path: str) -> str:
    """Public CDN URL — no Authorization header required."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    p = build_manifest(args.repo, args.date, args.overwrite)
    print(json.dumps({
        "manifest": str(p),
        "cdn_base": f"https://huggingface.co/datasets/{args.repo}/resolve/main/",
        "slot": assign_repo_slot(args.repo, args.date)
    }))
```

```python
# /opt/axentx/vanguard/train.py
import json
from pathlib import Path
from manifest import build_manifest, cdn_url

MANIFEST_DIR = Path(__file__).parent / "manifests"

def get_file_list(repo: str, date_folder: str):
    """Return CDN-only URLs from persisted manifest."""
    manifest_path = build_manifest(repo, date_folder)
    data = json.loads(manifest_path.read_text())
    return [cdn_url(repo, f["path"]) for f in data["files"]]

def make_dataloader(repo: str, date_folder: str, batch_size: int = 8):
    urls = get_file_list(repo, date_folder)
    # Use zero-auth CDN URLs with your parquet/tfdata/webdataset loader.
    # Do NOT call HF API inside training loop.
    # Placeholder:
    # dataset = load_parquet_urls(urls, projection={"prompt": str, "response": str})
    # return DataLoader(dataset, batch_size=batch_size)
    return urls
```

```python
# /opt/axentx/vanguard/launch_studio.py
#!/usr/bin/env python3
"""
Idempotent Lightning Studio reuse with lifecycle guard.
"""
import time
import os

# Prefer SDK import; fallback to CLI if unavailable
try:
    from lightning_sdk import Teamspace, Studio, Machine
    SDK_AVAILABLE = True
except Exception:
    SDK_AVAILABLE = False

TEAMSPACE = os.getenv("TEAMSPACE", "vanguard")
STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-train-l40s")

def get_or_start_studio():
    if not SDK_AVAILABLE:
        raise RuntimeError("lightning-sdk not available; install or configure CLI fallback.")

    ts = Teamspace(name=TEAMSPACE)
    running = [s for s in ts.studios if s.name == STUDIO_NAME and s.status == "running"]
    if running:
        return running[0]

    stopped = [s for s in ts.studios if s.name == STUDIO_NAME and s.status == "stopped"]
    if stopped:
        s = stopped[0]
        s.start(machine=Machine.L40S)
        _wait_for_running(s)
        return s

    # Create new (idempotent by deterministic name)
    studio = Studio.create(
        name=STUDIO_NAME,
        teamspace=TEAMSPACE,
        machine
