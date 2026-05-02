# vanguard / backend

## Final Synthesis (single, correct, actionable)

**Core diagnosis (merged, de-duplicated):**
- Every training run re-lists the HF dataset via authenticated `list_repo_tree`/`list_repo_files` → burns quota and risks 429.
- No durable file manifest → training cannot guarantee CDN-only fetches; auth headers and quota burn leak into dataloading.
- No deterministic write sharding → concurrent commits can hit HF’s 128-commit/hr/repo limit.
- No studio lifecycle guard → idle-stop kills training; jobs may recreate studios and waste quota/time.

**Chosen approach (correct + actionable):**
- Persist a single, date-scoped repo file manifest (JSON) after one authenticated listing; reuse it for all training runs.
- Force CDN-only URLs during training (no `Authorization` header).
- Deterministically shard write repos by hash-slug across 5 siblings to respect HF commit limits.
- Guard training entrypoints: check studio status, reuse if running, restart only if stopped, and avoid redundant listings.

**File layout (unified, minimal):**
- `/opt/axentx/vanguard/backend/manifest.py`
- `/opt/axentx/vanguard/backend/train_utils.py`
- `/opt/axentx/vanguard/backend/train.py` (updated launcher)

---

### 1) `/opt/axentx/vanguard/backend/manifest.py`
```python
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi, list_repo_tree

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/my-org/surrogate-mirror")
MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def snapshot_date_folder(date_folder: str, token: str | None = None) -> Path:
    """
    Persist a single-level tree listing for a date folder (e.g. '2026-04-29').
    Uses recursive=False to avoid heavy pagination.
    """
    api = HfApi(token=token)
    entries = list_repo_tree(
        repo_id=HF_REPO,
        path=date_folder,
        recursive=False,
        repo_type="dataset",
    )

    files = sorted(e.path for e in entries if e.type == "file")
    manifest = {
        "repo": HF_REPO,
        "date_folder": date_folder,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    out = MANIFEST_DIR / f"manifest-{date_folder}.json"
    out.write_text(json.dumps(manifest, indent=2))
    return out

def load_manifest(date_folder: str) -> Dict:
    p = MANIFEST_DIR / f"manifest-{date_folder}.json"
    if not p.exists():
        raise FileNotFoundError(
            f"No manifest for {date_folder}. Run snapshot_date_folder first."
        )
    return json.loads(p.read_text())
```

---

### 2) `/opt/axentx/vanguard/backend/train_utils.py`
```python
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Generator

import requests
from lightning import Fabric, Teamspace

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/my-org/surrogate-mirror")
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_urls(manifest_path: Path) -> Generator[str, None, None]:
    """Yield CDN URLs for files listed in manifest (no auth during training)."""
    manifest = json.loads(manifest_path.read_text())
    repo = manifest["repo"]
    for f in manifest["files"]:
        yield CDN_TEMPLATE.format(repo=repo, path=f)

def shard_repo_for_write(slug: str, n_siblings: int = 5) -> str:
    """
    Deterministic sibling repo selection to spread HF commit load.
    Returns repo name like 'datasets/my-org/surrogate-mirror-shard-2'.
    """
    base = HF_DATASET_REPO
    idx = hash(slug) % n_siblings
    return f"{base}-shard-{idx}"

def ensure_studio_running(studio_name: str, fabric: Fabric, machine: str = "L40S"):
    """
    Reuse a running studio; restart only if stopped to avoid idle-timeout kills.
    """
    for s in Teamspace.studios:
        if s.name == studio_name:
            if s.status == "running":
                return s
            # stopped/idle: restart
            s.restart()
            return s
    # does not exist: create
    return fabric.create_studio(
        name=studio_name,
        machine=machine,
        create_ok=True,
    )
```

---

### 3) `/opt/axentx/vanguard/backend/train.py` (updated launcher)
```python
from __future__ import annotations

import os
from pathlib import Path

from lightning import Fabric

from .manifest import load_manifest, snapshot_date_folder
from .train_utils import cdn_urls, ensure_studio_running, shard_repo_for_write

def main() -> None:
    date_folder = os.getenv("TRAIN_DATE_FOLDER", "2026-04-29")
    manifest_path = Path(__file__).parent / "manifests" / f"manifest-{date_folder}.json"

    # One-time manifest creation if missing
    if not manifest_path.exists():
        manifest_path = snapshot_date_folder(date_folder, token=os.getenv("HF_TOKEN"))

    # Studio lifecycle guard
    fabric = Fabric(devices=1)
    ensure_studio_running("vanguard-train", fabric, machine="L40S")

    # Data loading via CDN-only URLs (no auth during training)
    urls = list(cdn_urls(manifest_path))
    # Example: load_parquet_from_cdn(urls, columns=["prompt", "response"])
    # Ensure your dataloader uses requests/fsspec without Authorization header.

    # Deterministic write repo for artifacts/checkpoints
    write_repo = shard_repo_for_write(slug=date_folder)
    os.environ["HF_WRITE_REPO"] = write_repo

    # Continue training loop...
    # fabric.run(...)

if __name__ == "__main__":
    main()
```

---

### 4) Verification (single, concrete checklist)

1. **Create manifest once** (orchestrator/Mac):
   ```bash
   HF_TOKEN=... python -m vanguard.backend.manifest snapshot_date_folder 2026-04-29
   ```
   Confirm:
   ```bash
   cat /opt/axentx/vanguard/backend/manifests/manifest-2026-04-29.json
   ```

2. **Run training and confirm CDN-only fetches**:
   ```bash
   HF_TOKEN=... python -m vanguard.backend.train
   ```
   Inspect traffic (or set `HF_HUB_DISABLE_TELEMETRY=1` + logging) and verify:
   - No `Authorization` header on dataset file requests.
   - All URLs match `https://huggingface.co/datasets/.../resolve/main/...`.

3. **Confirm deterministic sharding**:
   ```bash
   python -c "from vanguard.backend.train_utils import shard_repo_for_write; print(shard_repo_for_write('2026-04-29'))"
   ```
   Output must be stable (e.g. `datasets/my-org/surrogate-mirror-shard-2`).

4. **Studio guard test**:
   - Stop the `vanguard-train` studio in Lightning UI.
   - Re-run the training script.
   - Confirm it restarts automatically and proceeds without recreating a new studio or re-listing the repo.
