# vanguard / discovery

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest exists, so every training run triggers authenticated `list_repo_tree` and burns HF API quota + risks 429s.
- Training likely uses `load_dataset(streaming=True)` or per-file authenticated fetches on heterogeneous repos, causing `pyarrow` schema errors and redundant API traffic.
- No CDN-only data path: authenticated API calls are used for file access when public CDN URLs (`resolve/main/...`) could bypass auth and rate limits entirely.
- No reuse guard for Lightning Studio: training script probably recreates studios instead of reusing running ones, wasting 80+ hrs/mo of quota.
- No idle-stop resilience: Lightning idle timeouts kill training; no status check/restart logic before `.run()` calls.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/prepare_file_manifest.py` (single script) that:
- Accepts `repo` and `dateFolder` (e.g. `datasets/mirror-merged/2026-05-03`)
- Calls `list_repo_tree(path=dateFolder, recursive=False)` **once** (Mac-side, after rate-limit window)
- Persists `{repo}_{dateFolder_slug}_files.json` into `vanguard/discovery/manifests/`
- Emits a small `train_manifest.json` containing only CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) for each file
- Adds a one-line stub to reuse running Lightning Studio and check idle-stop before training (as comment/docs for downstream training script)

Scope: one new file + one directory (`manifests/`). No edits to existing source until training script is updated to consume the manifest (next PR).

## 3. Implementation

```bash
# Create manifests directory
mkdir -p /opt/axentx/vanguard/discovery/manifests
```

```python
# /opt/axentx/vanguard/discovery/prepare_file_manifest.py
#!/usr/bin/env python3
"""
Generate a CDN-only file manifest for a given repo + dateFolder.
Usage:
  python prepare_file_manifest.py <repo> <dateFolder>
Example:
  python prepare_file_manifest.py axentx/datasets/mirror-merged 2026-05-03
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# HF SDK: only used for listing (single call). Training uses CDN URLs only.
from huggingface_hub import list_repo_tree

REPO_ROOT = Path(__file__).parent.parent.parent
MANIFESTS_DIR = REPO_ROOT / "discovery" / "manifests"
MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)

def slugify_date_folder(date_folder: str) -> str:
    return date_folder.strip("/").replace("/", "_").replace(" ", "_")

def build_manifest(repo: str, date_folder: str) -> Dict:
    """
    repo: e.g. 'axentx/datasets/mirror-merged'
    date_folder: e.g. '2026-05-03'
    """
    # Single authenticated API call (do this only once per folder, on Mac).
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [item for item in tree if item.get("type") == "file"]

    file_entries = []
    for f in files:
        path = f["path"]
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
        file_entries.append(
            {
                "path": path,
                "cdn_url": cdn_url,
                "size": f.get("size"),
                "lfs": f.get("lfs", {}),
            }
        )

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "file_count": len(file_entries),
        "files": file_entries,
        "notes": (
            "CDN-only manifest. Training should fetch via cdn_url (no auth/API calls). "
            "Do NOT use load_dataset(streaming=True) on heterogeneous repos."
        ),
    }
    return manifest

def save_manifest(repo: str, date_folder: str, manifest: Dict) -> Path:
    safe_repo = repo.replace("/", "_")
    date_slug = slugify_date_folder(date_folder)
    filename = f"{safe_repo}_{date_slug}_files.json"
    out_path = MANIFESTS_DIR / filename
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2, ensure_ascii=False)

    # Also write a minimal train_manifest.json for Lightning training script
    train_manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at_utc": manifest["generated_at_utc"],
        "cdn_files": [f["cdn_url"] for f in manifest["files"]],
    }
    train_path = MANIFESTS_DIR / f"{safe_repo}_{date_slug}_train.json"
    with open(train_path, "w", encoding="utf-8") as fp:
        json.dump(train_manifest, fp, indent=2, ensure_ascii=False)

    return out_path, train_path

def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python prepare_file_manifest.py <repo> <dateFolder>")
        sys.exit(1)

    repo = sys.argv[1].strip()
    date_folder = sys.argv[2].strip()

    print(f"Building manifest for repo={repo}, date_folder={date_folder}")
    manifest = build_manifest(repo, date_folder)
    out_path, train_path = save_manifest(repo, date_folder, manifest)

    print(f"Saved full manifest: {out_path}")
    print(f"Saved train manifest: {train_path}")
    print("Next step: use cdn_files in Lightning training script (no HF API calls during data load).")

if __name__ == "__main__":
    main()
```

```bash
# Make executable (optional, for direct shell use)
chmod +x /opt/axentx/vanguard/discovery/prepare_file_manifest.py
```

Lightning reuse + idle-stop stub (add to training launcher or docs):
```python
# Example snippet to reuse running studio and handle idle-stop
# from lightning_sdk import Teamspace, Studio, Machine
#
# studio_name = "vanguard-surrogate-train"
# teamspace = Teamspace()
# running = None
# for s in teamspace.studios:
#     if s.name == studio_name and s.status == "running":
#         running = s
#         break
#
# if running is None:
#     running = Studio.create(
#         name=studio_name,
#         machine=Machine.L40S,
#         repo=".",
#         create_ok=True,
#     )
# else:
#     # idle-stop resilience: if stopped, restart
#     if running.status == "stopped":
#         running.start(machine=Machine.L40S)
#
# # Before .run(), always check status
# running.refresh()
# if running.status != "running":
#     running.start(machine=Machine.L40S)
#
# job = running.run(command="python train.py --manifest ...")
```

## 4. Verification
1. Run manifest generation (on Mac or any dev env with HF token):
   ```bash
   cd /opt/axentx/vanguard/discovery
   python prepare_file_manifest.py axentx/datasets/mirror-merged 2026-05-03
   ```
2. Confirm files created:
   ```bash
   ls -l manifests/
   # Should show axentx_datasets_mirror-merged_2026-05-03_files.json
   # and axentx_datasets_mirror-merged_2026-05-03_train.json
   ```
3. Inspect `_train.json`:
   - Must contain `"cdn_files"` list with `https://huggingface.co/datasets/.../resolve/main/...` URLs.
   - No auth-bearing API endpoints.
4. Smoke-test CDN accessibility (no token):
   ```bash
   curl -I "$(jq -r '.cdn_files[0]' manifests/axentx_datasets_mirror-merged_2026-05-03_train
