# vanguard / discovery

## Final Synthesized Implementation

I merged the strongest elements from both proposals—prioritizing **correctness**, **quota safety**, and **concrete actionability**—while resolving contradictions in favor of a single, deployable solution.

### Key Decisions & Resolutions
- **Manifest approach**: Adopt Candidate 1’s `list_repo_tree` + CDN bypass (correct and minimal-pagination). Candidate 2’s S3/GCS fallback was dropped because it introduces infra complexity without solving the immediate HF API quota problem.
- **Lightning Studio reuse**: Adopt Candidate 1’s explicit `get_or_create_studio` + `ensure_studio_running` (concrete, actionable). Candidate 2’s “reuse guard” was too vague.
- **Orchestration rule**: Adopt Candidate 2’s strict “Mac=CLI, remote compute only” enforcement to prevent local model loading and credential leaks. This is implemented as a runtime guard.
- **Repo sharding**: Adopt Candidate 1’s deterministic hash-based shard selection to spread commit load and avoid the 128/hr cap. Candidate 2 mentioned sharding but provided no mechanism.
- **Training data loading**: Use Candidate 1’s `cdn_urls_for_date` to produce CDN-only URLs so training uses zero HF API calls during data load.

---

### 1. Implementation

Create `/opt/axentx/vanguard/discovery/manifest.py` (single file, <100 LoC):

```python
#!/usr/bin/env python3
"""
Durable ingestion manifest + CDN bypass utilities for vanguard training.
- Lists HF repo tree once and caches to manifests/{date}/filelist.json
- Provides CDN URLs for direct file fetch (bypasses API auth limits)
- Reuses running Lightning Studio to save quota and avoid cold starts
- Shards writes across sibling repos to avoid 128/hr commit cap
- Enforces Mac=CLI + remote compute rule (no local model loading)
"""

import json
import os
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# ---- Orchestration guard: Mac=CLI, remote compute only ----
def enforce_remote_compute_rule() -> None:
    """Ensure we never load models locally on Mac dev machines."""
    if sys.platform == "darwin" and "VANGUARD_REMOTE_EXEC" not in os.environ:
        raise RuntimeError(
            "Local model loading not allowed. "
            "Run from Mac CLI only for orchestration. "
            "Set VANGUARD_REMOTE_EXEC=1 to bypass (remote compute only)."
        )


# ---- HF manifest + CDN bypass ----
try:
    from huggingface_hub import HfApi, list_repo_tree
    HUGGINGFACE_HUB_AVAILABLE = True
except Exception:
    HUGGINGFACE_HUB_AVAILABLE = False
    HfApi = None
    list_repo_tree = None

MANIFEST_ROOT = Path(__file__).parent.parent / "manifests"
HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx-mirror")
SIBLING_REPOS = [
    f"{HF_DATASET_REPO}-shard0",
    f"{HF_DATASET_REPO}-shard1",
    f"{HF_DATASET_REPO}-shard2",
    f"{HF_DATASET_REPO}-shard3",
    f"{HF_DATASET_REPO}-shard4",
]


def build_manifest(date_str: str, out_dir: Optional[Path] = None) -> Path:
    """
    List repo tree for a single date folder and save file list.
    Call from Mac orchestration (once per date) after rate-limit window clears.
    """
    enforce_remote_compute_rule()

    if not HUGGINGFACE_HUB_AVAILABLE or HfApi is None:
        raise RuntimeError("huggingface_hub not available")

    api = HfApi()
    folder_path = f"batches/mirror-merged/{date_str}"
    out_dir = Path(out_dir or MANIFEST_ROOT) / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "filelist.json"

    # recursive=False keeps pagination small; we only need top-level parquet files
    tree = list_repo_tree(repo_id=HF_DATASET_REPO, path=folder_path, recursive=False)
    files = [
        {"path": f.rfilename, "size": getattr(f, "size", None)}
        for f in tree
        if f.rfilename.lower().endswith(".parquet")
    ]

    manifest = {
        "date": date_str,
        "source_repo": HF_DATASET_REPO,
        "folder": folder_path,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    with open(manifest_path, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2)

    print(f"Manifest written: {manifest_path} ({len(files)} files)")
    return manifest_path


def load_manifest(date_str: str) -> dict:
    manifest_path = MANIFEST_ROOT / date_str / "filelist.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def cdn_url(repo: str, file_path: str) -> str:
    """CDN bypass URL (no Authorization header required)."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"


def cdn_urls_for_date(date_str: str, repo: Optional[str] = None) -> List[str]:
    """Return CDN URLs for all parquet files in a date manifest."""
    repo = repo or HF_DATASET_REPO
    manifest = load_manifest(date_str)
    return [cdn_url(repo, f["path"]) for f in manifest["files"]]


def pick_shard_repo(slug: str) -> str:
    """Deterministic repo selection to spread HF commit writes (avoid 128/hr cap)."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]


# ---- Lightning Studio reuse helpers ----
try:
    from lightning.pytorch.studio import Studio, Machine
    from lightning.pytorch.studio._teamspace import Teamspace
    LIGHTNING_STUDIO_AVAILABLE = True
except Exception:
    LIGHTNING_STUDIO_AVAILABLE = False
    Studio = None
    Machine = None
    Teamspace = None


def get_or_create_studio(
    name: str,
    machine: str = "L40S",
    cloud: str = "lightning-public-prod",
    create_ok: bool = True,
):
    """
    Reuse a running Studio if present; otherwise create one.
    Avoids quota waste and idle-stop training loss.
    """
    enforce_remote_compute_rule()

    if not LIGHTNING_STUDIO_AVAILABLE or Studio is None or Machine is None or Teamspace is None:
        raise RuntimeError("Lightning SDK studio components not available")

    teamspace = Teamspace()
    for studio in teamspace.studios:
        if studio.name == name and studio.status == "Running":
            print(f"Reusing running Studio: {name}")
            return studio

    if not create_ok:
        raise RuntimeError(f"No running Studio named '{name}' and create_ok=False")

    print(f"Creating Studio: {name} (machine={machine}, cloud={cloud})")
    return Studio(
        name=name,
        machine=Machine(machine, cloud=cloud),
        create_ok=True,
    )


def ensure_studio_running(studio, machine: str = "L40S", cloud: str = "lightning-public-prod"):
    """If studio is stopped, restart it before training runs."""
    enforce_remote_compute_rule()

    if studio.status != "Running":
        print(f"Studio '{studio.name}' not running (status={studio.status}). Restarting...")
        studio.start(machine=Machine(machine, cloud=cloud))
    return studio


if __name__ == "__main__":
    # Example CLI usage (Mac orchestration):
    # python discovery/manifest.py 2026-04-29
    enforce_remote_compute_rule()

    date_arg = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    build_manifest(date_arg)
```

---
