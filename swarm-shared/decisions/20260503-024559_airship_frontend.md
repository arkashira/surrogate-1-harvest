# airship / frontend

## Final Integrated Plan (Best of Both Candidates)

**Core improvement (≤2h):**  
Add a CDN-first training slice that (1) lists one HF date-folder via a backend API, (2) persists `file-list.json`, (3) reuses a running Lightning Studio (restart if idle-stopped), (4) trains from CDN URLs only, and (5) projects each file to `{prompt, response}` at parse time. Eliminates HF API 429s during data loading and prevents idle-stop quota waste.

---

## Concrete Implementation Plan (timeboxed)

1. **Explore repo layout** (5m)  
   - Confirm `surrogate/` frontend and existing training scripts.

2. **Add backend: HF file-list endpoint** (20m)  
   - `POST /api/training/file-list` with `{ repo, path }`  
   - Calls HF API once (`list_repo_tree(..., recursive=False)`), returns + persists `file-list.json`.

3. **Add minimal frontend UI** (25m)  
   - Page/modal: repo + date-folder picker → “Generate file list”  
   - Shows list + “Start CDN training” button.

4. **Add backend: CDN training launcher** (20m)  
   - `POST /api/training/start-cdn`  
   - Loads persisted `file-list.json`  
   - Reuses running Lightning Studio named `surrogate-cdn-train`; if not running, starts one (L40S/H200 priority, fallback to free-tier).  
   - Detects idle-stop and restarts studio before run.

5. **Add/modify training script slice** (20m)  
   - Accept file-list path or inline list.  
   - Data loader uses CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header.  
   - Parser projects each file to `{prompt, response}` only (strict schema).

6. **Validation & smoke test** (20m)  
   - Generate list → verify JSON.  
   - Start training slice → confirm CDN fetches and schema projection.  
   - Confirm zero HF API calls during data loading (logs).

---

## Integrated Code Snippets

### 1. Backend: HF file-list endpoint (FastAPI)

```python
# surrogate/api/training.py
import json
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from huggingface_hub import HfApi

from ..config import DATA_DIR

router = APIRouter()
HF_API = HfApi()

@router.post("/file-list")
async def generate_file_list(repo: str, path: str):
    """
    List one folder (non-recursive) from a HF dataset repo.
    Returns and persists file-list.json for CDN-only training.
    """
    try:
        tree = HF_API.list_repo_tree(repo=repo, path=path, recursive=False)
        files = sorted(f.rfilename for f in tree if f.rfilename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HF list failed: {exc}")

    payload = {
        "repo": repo,
        "path": path,
        "files": files,
    }

    out_path = Path(DATA_DIR) / "file-list.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))

    return {"ok": True, "out": str(out_path), "count": len(files), "files": files}
```

### 2. Backend: CDN training launcher (Lightning reuse + idle restart)

```python
# surrogate/api/training.py (continued)
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..training.cdn_train import run_cdn_training

router = APIRouter()

# Lightweight abstraction over Lightning Studio operations.
# Adapt to your org's actual Teamspace/studio client.
class StudioManager:
    def __init__(self, org_name: str):
        self.org_name = org_name

    def find_running(self, name: str):
        # Placeholder: replace with real client call
        # e.g., teamspace.studios where status == "running"
        return None

    def start_or_create(self, name: str, machine: str = "L40S"):
        # Placeholder: create/start studio and return handle
        # Return minimal stub with .name, .status, .start()
        class StubStudio:
            def __init__(self, name, machine):
                self.name = name
                self.machine = machine
                self.status = "running"

            def start(self, machine=None):
                self.status = "running"
                return self

        return StubStudio(name, machine)

    def ensure_running(self, name: str, machine: str = "L40S"):
        studio = self.find_running(name)
        if studio is None or studio.status != "running":
            studio = self.start_or_create(name, machine=machine)
        return studio

_manager = StudioManager(org_name="surrogate-org")

@router.post("/start-cdn")
async def start_cdn_training():
    list_path = Path(DATA_DIR) / "file-list.json"
    if not list_path.exists():
        raise HTTPException(status_code=400, detail="file-list.json not found. Generate it first.")

    cfg = json.loads(list_path.read_text())
    repo = cfg["repo"]
    files = cfg["files"]

    # Reuse or start studio
    studio = _manager.ensure_running("surrogate-cdn-train", machine="L40S")

    # Run CDN training slice (non-blocking recommended in production)
    result = run_cdn_training(repo=repo, files=files, studio_name=studio.name)
    return {"ok": True, "studio": studio.name, "status": studio.status, "result": result}
```

### 3. Training slice: CDN fetches + schema projection

```python
# surrogate/training/cdn_train.py
import json
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def load_via_cdn(repo: str, path: str) -> bytes:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

def project_to_prompt_response(raw_bytes: bytes, path: str) -> List[Dict[str, str]]:
    """
    Project file to {prompt, response} only.
    Supports JSONL (one record per line) and plain JSON list.
    Extend with pyarrow for parquet as needed.
    """
    text = raw_bytes.decode("utf-8").strip()
    if not text:
        return []

    # Try JSONL first
    if "\n" in text:
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                records.append(obj)
            except json.JSONDecodeError:
                continue
    else:
        # Try single JSON array
        try:
            records = json.loads(text)
        except json.JSONDecodeError:
            records = []

    projected = [
        {"prompt": r.get("prompt", ""), "response": r.get("response", "")}
        for r in records
        if isinstance(r, dict)
    ]
    return projected

def run_cdn_training(repo: str, files: List[str], studio_name: str = None) -> Dict:
    """
    Lightweight CDN-only slice. Returns stats and writes slice.parquet.
    Integrate this into your Lightning DataModule/train loop.
    """
    dataset = []
    failed = []
    for f in files:
        try:
            raw = load_via_cdn(repo, f)
            projected = project_to_prompt_response(raw, f)
            dataset.extend(projected)
        except Exception as exc:
            failed.append({"file": f, "error": str(exc)})
            continue

    out_dir = Path("training_slice")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "slice.parquet"
    pd.DataFrame(dataset).to_parquet(out_path, index=False)

    return {
        "repo": repo,
       
