# vanguard / quality

## Final Synthesized Implementation (Correct + Actionable)

**Core principle**: eliminate all Hugging Face API calls during training, reuse Lightning Studio instances, and auto-recover from idle stops.  
The final design merges the strongest, most concrete parts from both candidates and resolves contradictions in favor of reliability and minimal quota burn.

---

## 1. Diagnosis (resolved)

- **Problem**: Repeated HF repo listings burn API quota and cause 429s.  
  **Fix**: single durable file manifest per dateFolder; never re-list during training.
- **Problem**: Data loader uses HF client/`datasets` and risks rate limits.  
  **Fix**: enforce CDN-only fetches (`/resolve/main/` URLs) at training time.
- **Problem**: Launcher recreates Lightning Studio instances and wastes quota.  
  **Fix**: explicit reuse + restart of stopped studios; never recreate if a named studio exists.
- **Problem**: Idle-stop kills training with no recovery.  
  **Fix**: ensure studio is running before submit; optionally add lightweight keepalive or checkpointing in train loop.

---

## 2. Architecture (single PR, <2h)

```
vanguard/
├── training/
│   ├── manifest.py          # snapshot + load manifest; CDN URL builder
│   ├── launcher.py          # studio reuse + idle resilience + submit
│   └── train.py             # CDN-only loader; accepts MANIFEST_JSON
frontend/
└── src/lib/
    ├── hfManifest.ts        # optional: same logic for UI previews (kept minimal)
    └── lightningStudio.ts   # optional: thin SDK wrapper for frontend reuse
```

**Key decisions**:
- Manifest is the single source of truth per dateFolder; created once, reused forever (until explicit refresh).
- Training uses only CDN URLs; no `datasets` or HF API calls in the hot loop.
- Launcher never creates duplicate studios; it restarts stopped ones and reuses running ones.
- Launcher is the only place that talks to HF API (for manifest creation) and Lightning SDK.

---

## 3. Implementation

### vanguard/training/manifest.py
```python
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, TypedDict

import requests

HF_REPO = os.getenv("HF_REPO", "datasets/your-org/your-repo")
HF_TOKEN = os.getenv("HF_TOKEN", "")  # only used for manifest creation

class FileEntry(TypedDict):
    path: str
    size: int
    sha: str

def _repo_tree(path: str = "") -> List[FileEntry]:
    url = f"https://huggingface.co/api/datasets/{HF_REPO}/tree"
    params = {"path": path, "recursive": 0}
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    items = resp.json()
    return [i for i in items if i.get("type") == "file"]  # type: ignore[return-value]

def _manifest_dir() -> Path:
    base = Path(os.getenv("MANIFEST_DIR", Path(__file__).parent.parent / "manifests"))
    base.mkdir(parents=True, exist_ok=True)
    return base

def get_or_create_manifest(date_folder: str) -> List[FileEntry]:
    """
    Return persisted manifest for date_folder.
    Creates it once via HF API if missing.
    """
    p = _manifest_dir() / f"{date_folder}.json"
    if p.exists():
        return json.loads(p.read_text())

    items = _repo_tree(date_folder)
    files = [FileEntry(path=i["path"], size=i["size"], sha=i["sha"]) for i in items]
    p.write_text(json.dumps(files, indent=2))
    return files

def to_cdn_urls(files: List[FileEntry]) -> List[str]:
    return [
        f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f['path']}"
        for f in files
    ]
```

### vanguard/training/launcher.py
```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Dict, Any

from .manifest import get_or_create_manifest, to_cdn_urls

try:
    from lightningai import Studio, Teamspace, Machine
    _HAS_LIGHTNING = True
except Exception:
    _HAS_LIGHTNING = False
    # Fallback for environments without Lightning SDK (tests/local runs)

TEAMSPACE = os.getenv("LIGHTNING_TEAMSPACE", "your-teamspace")
STUDIO_NAME = os.getenv("LIGHTNING_STUDIO", "vanguard-training")
MACHINE = os.getenv("LIGHTNING_MACHINE", "L40S")

def _ensure_lightning() -> None:
    if not _HAS_LIGHTNING:
        raise RuntimeError("Lightning SDK required for studio operations.")

def get_or_create_studio() -> Studio:
    _ensure_lightning()
    teamspace = Teamspace(TEAMSPACE)
    studios = teamspace.studios()

    running = next((s for s in studios if s.name == STUDIO_NAME and s.status == "Running"), None)
    if running:
        return running

    stopped = next((s for s in studios if s.name == STUDIO_NAME and s.status == "Stopped"), None)
    if stopped:
        stopped.start(machine=Machine[MACHINE])
        return stopped

    return Studio.create(
        name=STUDIO_NAME,
        teamspace=TEAMSPACE,
        machine=Machine[MACHINE],
        create_ok=True,
    )

def ensure_running(studio: Studio) -> Studio:
    _ensure_lightning()
    s = Studio.get(studio.id)
    if s.status != "Running":
        s.start(machine=Machine[MACHINE])
    return s

def run_training(date_folder: str, *, block: bool = True, extra_env: Dict[str, str] | None = None) -> Dict[str, Any]:
    """
    End-to-end launcher:
    1) Manifest (single API call per date_folder, persisted)
    2) Studio reuse + idle resilience
    3) Submit CDN-only training job
    """
    files = get_or_create_manifest(date_folder)
    cdn_urls = to_cdn_urls(files)
    manifest_json = json.dumps(cdn_urls)

    studio = get_or_create_studio()
    running_studio = ensure_running(studio)

    # Prefer file-based template when available; fallback to inline script
    train_script_path = os.path.join(os.path.dirname(__file__), "train.py")
    if os.path.exists(train_script_path):
        cmd = [sys.executable, train_script_path]
        env = {
            **os.environ,
            "MANIFEST_JSON": manifest_json,
            "DATE_FOLDER": date_folder,
        }
        if extra_env:
            env.update(extra_env)
        # Run locally (or via studio.run with file upload)
        if block:
            subprocess.run(cmd, env=env, check=True)
        proc = subprocess.Popen(cmd, env=env)
        return {"pid": proc.pid, "studio_id": running_studio.id, "files": len(files)}

    # Inline fallback (for studio.run without file upload)
    train_script = (
        "import json, os, io, pyarrow.parquet as pq, requests; "
        "urls = json.loads(os.environ['MANIFEST_JSON']); "
        "[pq.read_table(io.BytesIO(requests.get(u).content)).select(['prompt','response']).to_pandas() for u in urls]; "
        "print('done')"
    )
    run = running_studio.run(
        name=f"train-{date_folder}",
        command=[sys.executable, "-c", train_script],
        machine=MACHINE,
        env={"MANIFEST_JSON": manifest_json, **dict(extra_env or {})},
    )
    if block:
        run.wait()
    return {"runId": run.id, "studioId": running_studio.id, "files": len(files)}
```

### vanguard/training/train.py
```python
import json
import os
import io
import pyarrow.parquet as pq
import requests

def main() -> None:
    manifest_json = os.environ.get("MANIFEST_JSON")
    if not manifest_json:
        raise RuntimeError("MANIFEST_JSON environment variable is required.")

    urls = json.loads
