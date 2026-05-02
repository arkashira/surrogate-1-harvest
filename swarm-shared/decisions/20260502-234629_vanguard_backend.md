# vanguard / backend

## Final Synthesized Implementation

**Core diagnosis (unified):**  
- No persisted manifest → repeated `list_repo_tree`/`list_repo_files` risks HF API 429.  
- Training uses HF path-based loading instead of CDN URLs → redundant auth/rate-limit + slower throughput.  
- Lightning Studio lifecycle is ad-hoc (recreate) → quota burn + cold-start latency.  
- No deterministic repo selection for HF commit-cap mitigation (128/hr/repo).  
- No idle-stop guard → `.run()` fails when Studio is stopped.

**Chosen approach:**  
Adopt Candidate 2’s orchestrator-first model (single entrypoint, manifest persisted, CDN-only training) **plus** Candidate 1’s `pick_repo` deterministic selector and explicit `ensure_studio_running` guard. Remove contradictions by making the orchestrator the single source of truth: it lists once, writes a deterministic manifest, picks repos via hash, reuses or restarts Studios, and launches training with `--manifest`. Training code consumes only CDN URLs and projects heterogeneous files to `{prompt,response}`.

---

## 1. Backend module: `/opt/axentx/vanguard/backend/manifest.py`

```python
# /opt/axentx/vanguard/backend/manifest.py
import json
import hashlib
from pathlib import Path
from typing import List, Dict, Optional

try:
    from huggingface_hub import list_repo_tree, HfApi
except ImportError:
    list_repo_tree = None
    HfApi = None


def pick_repo(slug: str, n: int = 5, prefix: str = "vanguard-data-") -> str:
    """Deterministic sibling repo selector to spread HF commit cap."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n
    return f"{prefix}{idx:02d}"


def build_and_save_manifest(repo: str, folder: str, out_path: str) -> List[str]:
    """
    List files in folder (non-recursive) and save manifest JSON.
    Returns list of file paths.
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not available")
    tree = list_repo_tree(repo_id=repo, path=folder, recursive=False)
    paths = sorted(entry.path for entry in tree if entry.type == "file")
    manifest = {
        "repo": repo,
        "folder": folder,
        "files": paths,
    }
    Path(out_path).write_text(json.dumps(manifest, indent=2))
    return paths


def resolve_cdn_urls(manifest_path: str) -> List[str]:
    """Convert manifest file paths to public CDN URLs (no auth)."""
    manifest = json.loads(Path(manifest_path).read_text())
    base = f"https://huggingface.co/datasets/{manifest['repo']}/resolve/main"
    return [f"{base}/{p}" for p in manifest["files"]]


def ensure_studio_running(studio) -> None:
    """Idempotent guard: if studio stopped, restart it."""
    if not hasattr(studio, "status"):
        return
    if getattr(studio, "status", None) != "running":
        # studio.start(machine=...) should be supported; pass machine if available
        machine = getattr(studio, "machine", None)
        studio.start(machine=machine)
```

---

## 2. Orchestrator: `/opt/axentx/vanguard/backend/orchestrate_train.py`

```python
#!/usr/bin/env python3
# /opt/axentx/vanguard/backend/orchestrate_train.py
"""
Orchestrator: list once, reuse studio, launch training with CDN-only data.
"""
import json
import os
import sys
from pathlib import Path

import lightning as L
from huggingface_hub import list_repo_tree

from vanguard.backend.manifest import (
    build_and_save_manifest,
    resolve_cdn_urls,
    ensure_studio_running,
    pick_repo,
)

HF_REPO_PREFIX = os.getenv("HF_DATASET_REPO_PREFIX", "axentx/vanguard-data")
DATE_FOLDER = os.getenv("TRAIN_DATE_FOLDER", "batches/mirror-merged/2026-05-02")
MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

# Deterministic repo selection for commit-cap mitigation
# If HF_REPO is set, use it; else pick sibling repo based on date_folder
if "HF_DATASET_REPO" in os.environ:
    repo = os.environ["HF_DATASET_REPO"]
else:
    # Use date_folder as slug to pick deterministic sibling repo
    repo = pick_repo(DATE_FOLDER, n=5, prefix="vanguard-data-")
    # If prefix doesn't include namespace, prepend from env or default
    if "/" not in repo:
        repo = f"{HF_REPO_PREFIX.split('/')[0]}/{repo}"

MANIFEST_PATH = MANIFEST_DIR / f"{DATE_FOLDER.replace('/', '_')}.json"


def list_files_once() -> list[str]:
    if MANIFEST_PATH.exists():
        data = json.loads(MANIFEST_PATH.read_text())
        return data.get("files", [])
    # single non-recursive list call
    paths = build_and_save_manifest(repo, DATE_FOLDER, str(MANIFEST_PATH))
    return paths


def main() -> None:
    # 1) Build manifest once
    list_files_once()
    urls = resolve_cdn_urls(str(MANIFEST_PATH))
    if not urls:
        print("No files found in manifest; aborting.", file=sys.stderr)
        sys.exit(1)

    # 2) Reuse or restart Lightning Studio
    team = L.Teamspace()
    studio_name = os.getenv("STUDIO_NAME", "vanguard-l40s-train")
    studio = next((s for s in team.studios if s.name == studio_name), None)

    if studio is None:
        studio = L.Studio.create(
            name=studio_name,
            machine=L.Machine.L40S,
            create_ok=True,
        )
    else:
        ensure_studio_running(studio)

    # 3) Launch training with manifest (CDN-only)
    cmd = [
        "python",
        str(Path(__file__).parent / "train.py"),
        "--manifest",
        str(MANIFEST_PATH),
        "--repo",
        repo,
    ]
    # Pass any extra args through
    if len(sys.argv) > 1:
        cmd.extend(sys.argv[1:])

    studio.run(" ".join(cmd), wait=False)
    print(f"Launched training in studio '{studio_name}' with manifest {MANIFEST_PATH.name}")


if __name__ == "__main__":
    main()
```

---

## 3. Training script: `/opt/axentx/vanguard/backend/train.py`

```python
# /opt/axentx/vanguard/backend/train.py
import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from datasets import load_dataset


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=str, required=True)
    parser.add_argument("--repo", type=str, required=True)
    parser.add_argument("--output_dir", type=str, default="./outputs")
    # Add other training args as needed
    return parser.parse_args()


def project_to_prompt_response(record: Dict) -> Dict[str, str]:
    """
    Project heterogeneous file record to {prompt, response}.
    Customize per your schema; this is a minimal robust fallback.
    """
    # Common keys
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "completion", "output", "answer", "assistant"}

    prompt = None
    response = None

    for k in prompt_keys:
        if k in record and record[k] is not None:
            prompt = str(record[k]).strip()
            break
    for k in response_keys:
        if k in record and record[k] is not None:
            response = str(record[k]).strip()
            break

    # Fallbacks
    if prompt is None:
        # try to use first text-like field
        for v in record.values():
            if isinstance(v, str) and v.strip():
                prompt = v.strip()
                break
    if response is None:
        response = ""
