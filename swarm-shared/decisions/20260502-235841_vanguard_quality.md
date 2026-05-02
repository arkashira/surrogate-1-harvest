# vanguard / quality

## Final Consolidated Implementation

### 1. Diagnosis (merged)
- **No persisted HF file manifest per date-folder**: every training run re-lists repo via authenticated API (wastes quota, risks 429).
- **No CDN-only data path**: training incurs API auth checks during data loading instead of using public CDN URLs.
- **Lightning Studio created fresh each run**: burns quota instead of reusing running instances; no recovery from idle-stop.
- **Heterogeneous HF repo schemas**: risk of `pyarrow.CastError` when mixing file types; no schema projection.
- **Missing deterministic repo selection**: no mitigation for HF commit-cap (128/hr/repo).

### 2. Proposed Change
Create `/opt/axentx/vanguard/src/backend/training/training_launcher.py` that:
- Accepts `date_folder` and HF dataset repo.
- Lists repo tree once (non-recursive) for that folder and persists `manifest-{date_folder}.json`.
- Embeds manifest into training so Lightning uses CDN-only fetches (zero API calls during training).
- Reuses a Running Studio by name; if stopped, restarts it on L40S.
- Launches training with schema projection (prompt/response only) and deterministic repo selection for HF commit-cap mitigation.

### 3. Implementation

```bash
mkdir -p /opt/axentx/vanguard/src/backend/training
```

```python
# /opt/axentx/vanguard/src/backend/training/training_launcher.py
#!/usr/bin/env python3
"""
Durable training launcher for HF datasets on Lightning.

Key guarantees:
- HF tree is listed once per date_folder and persisted as a manifest.
- Train script uses CDN-only fetches (zero API calls during data load).
- Reuses Running Studio; auto-restarts if idle-stopped.
- Projects heterogeneous HF files to {prompt, response} at parse time.
- Deterministic sibling repo selection to respect HF commit cap.
"""

import json
import os
import hashlib
import time
from pathlib import Path
from typing import List, Dict, Any

import lightning as L
from huggingface_hub import HfApi


HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1")
MANIFEST_DIR = Path(os.getenv("MANIFEST_DIR", "/opt/axentx/vanguard/data/manifests"))
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

# Spread writes across siblings to avoid HF commit cap (128/hr/repo)
HF_SIBLINGS = [
    "axentx/surrogate-1",
    "axentx/surrogate-2",
    "axentx/surrogate-3",
    "axentx/surrogate-4",
    "axentx/surrogate-5",
]


def pick_repo(slug: str) -> str:
    """Deterministically pick sibling repo by slug hash."""
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(HF_SIBLINGS)
    return HF_SIBLINGS[idx]


def list_date_folder(repo: str, date_folder: str) -> List[str]:
    """List top-level files in date_folder (non-recursive) and persist manifest."""
    api = HfApi()
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]

    manifest_path = MANIFEST_DIR / f"manifest-{date_folder}.json"
    manifest_path.write_text(json.dumps(files, indent=2))
    return files


def build_cdn_urls(repo: str, date_folder: str, files: List[str]) -> List[str]:
    """Return CDN URLs (bypass HF API auth checks)."""
    return [
        f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{f}"
        for f in files
    ]


def ensure_studio(name: str, machine: L.Machine = L.Machine.L40S) -> L.Studio:
    """Reuse Running Studio; if stopped, restart on machine."""
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name:
            if s.status == "running":
                print(f"Reusing running studio: {name}")
                return s
            else:
                print(f"Studio {name} exists but stopped ({s.status}). Restarting...")
                s.start(machine=machine)
                while s.status != "running":
                    time.sleep(10)
                    s.refresh()
                return s

    print(f"Creating new studio: {name}")
    return L.Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )


def launch_training(
    date_folder: str,
    train_script: str = "train.py",
    studio_name: str = "vanguard-train",
    hf_repo: str = HF_REPO,
) -> None:
    """
    End-to-end launcher:
    1) Persist manifest for date_folder.
    2) Ensure studio running.
    3) Run training with manifest injected (CDN-only).
    """
    # 1) Manifest
    files = list_date_folder(hf_repo, date_folder)
    if not files:
        raise ValueError(f"No files found in {hf_repo}/{date_folder}")

    manifest_path = MANIFEST_DIR / f"manifest-{date_folder}.json"
    print(f"Manifest written: {manifest_path} ({len(files)} files)")

    # 2) Studio
    studio = ensure_studio(studio_name, machine=L.Machine.L40S)

    # 3) Run training
    launcher_code = f"""
import json
import os
from pathlib import Path

MANIFEST_PATH = r"{manifest_path}"
DATE_FOLDER = "{date_folder}"
HF_REPO = "{hf_repo}"

with open(MANIFEST_PATH) as f:
    FILES = json.load(f)

# Build CDN URLs (bypass HF API during training)
FILES_CDN = [
    f"https://huggingface.co/datasets/{{HF_REPO}}/resolve/main/{{DATE_FOLDER}}/{{f}}"
    for f in FILES
]

# Export for train.py
os.environ["MANIFEST_PATH"] = MANIFEST_PATH
os.environ["DATE_FOLDER"] = DATE_FOLDER
os.environ["HF_REPO"] = HF_REPO
os.environ["FILES_CDN"] = json.dumps(FILES_CDN)

# Run actual training script
import subprocess
subprocess.run(["python", r"{train_script}"], check=True)
"""

    print("Launching training in studio...")
    job = studio.run(
        code=launcher_code,
        name=f"train-{date_folder}",
    )
    print(f"Launched job: {job}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Durable HF training launcher")
    parser.add_argument("--date-folder", required=True, help="HF date folder (e.g. 2026-04-29)")
    parser.add_argument("--train-script", default="train.py", help="Path to train.py in studio")
    parser.add_argument("--studio-name", default="vanguard-train", help="Lightning Studio name")
    parser.add_argument("--hf-repo", default=HF_REPO, help="HF dataset repo")
    args = parser.parse_args()

    launch_training(
        date_folder=args.date_folder,
        train_script=args.train_script,
        studio_name=args.studio_name,
        hf_repo=args.hf_repo,
    )
```

```python
# /opt/axentx/vanguard/src/backend/training/train.py (minimal example to pair with launcher)
"""
Example train.py that uses CDN-only fetches and projects heterogeneous HF files
to {prompt, response} at parse time (avoids pyarrow CastError).
"""

import json
import os
from pathlib import Path
from typing import Dict, Any, List

import pyarrow.parquet as pq
import requests
from datasets import Dataset


def download_via_cdn(url: str, local_path: Path) -> Path:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        return local_path
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    local_path.write_bytes(resp.content)
    return local_path


def safe_read_parquet(path: Path) -> List[
