# airship / frontend

## Final Synthesized Implementation  
*(Best parts of Candidate 1 + Candidate 2, contradictions resolved for correctness + concrete actionability)*

---

## Goal (non-negotiable)
- **Training iteration < 2 minutes** (no HF API 429, no Lightning idle-stop kills).  
- **Architecture rule preserved**: Mac orchestrates; remote trains.  
- **Frontend-safe**: launcher UI + backend API; no direct HF API calls during training.  
- **Deployable < 2 hours** with copy-pasteable code.

---

## High-level Design (resolved choices)
| Decision | Candidate 1 | Candidate 2 | Final (correct + actionable) |
|---|---|---|---|
| Manifest generation | Python script on Mac | JS script on Mac + optional API endpoint | **Python script on Mac** (more reliable, fewer deps). Add optional lightweight FastAPI endpoint for frontend listing. |
| CDN bypass | Direct `requests`/`aiohttp` in DataLoader | Same intent | **Keep Candidate 1 CDN fetch** (fast, no HF API). |
| Lightning reuse | Reuse running studio; start if stopped | Same intent | **Keep Candidate 1 reuse logic** + add 30s pre-check to avoid duplicates. |
| Frontend integration | Not addressed | Vue component + `/api/training/run` | **Add minimal FastAPI endpoints** (`/api/training/file-list`, `/api/training/run`) so frontend can list datasets and launch runs. |
| Manifest format | Full metadata (folder, file, cdn_url) | Minimal file list | **Use Candidate 1 full metadata** (gives CDN URLs directly to training). Provide minimal list via API for frontend. |
| Crontab entry | Prefetch daily | Not specified | **Keep Candidate 1 cron** (prefetch after dataset updates). |

---

## Implementation Plan (concrete steps)

### 1) Pre-cache manifest on Mac orchestrator (10–15 min)
- Script: `scripts/prefetch_manifest.py` (Python, uses `huggingface_hub`).  
- Runs locally or via cron; outputs `training/file_manifest.json` with full CDN URLs.  
- Also writes minimal `training/file_list.json` for frontend/API.

```python
# scripts/prefetch_manifest.py
#!/usr/bin/env python3
import json, os, sys
from huggingface_hub import HfApi

REPO = os.getenv("HF_REPO", "your-org/surrogate-dataset")
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else "2026-04-29"
OUT_DIR = "training"
MANIFEST = os.path.join(OUT_DIR, "file_manifest.json")
LIST_OUT = os.path.join(OUT_DIR, "file_list.json")

os.makedirs(OUT_DIR, exist_ok=True)
api = HfApi()

folders = [f for f in api.list_repo_tree(REPO, path=DATE_FOLDER, recursive=False) if f.type == "directory"]
manifest = []
file_list = []

for folder in folders:
    files = api.list_repo_tree(REPO, path=folder.path, recursive=False)
    for f in files:
        if f.type == "file" and f.path.endswith((".parquet", ".jsonl")):
            cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{f.path}"
            manifest.append({"folder": folder.path, "file": f.path, "cdn_url": cdn_url})
            file_list.append(f.path)

with open(MANIFEST, "w") as f:
    json.dump(manifest, f, indent=2)
with open(LIST_OUT, "w") as f:
    json.dump({"files": file_list}, f, indent=2)

print(f"Saved {len(manifest)} files to {MANIFEST}")
```

Crontab (on Mac):
```cron
SHELL=/bin/bash
0 3 * * * cd /opt/axentx/airship && python3 scripts/prefetch_manifest.py 2026-04-29 >> logs/prefetch.log 2>&1
```

---

### 2) Backend API (FastAPI) — lightweight, runs on orchestrator
- Provides `/api/training/file-list` and `/api/training/run`.  
- `POST /run` triggers Lightning Studio reuse flow and passes manifest path.

```python
# api/training_api.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess, json, os

app = FastAPI()
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "..", "training", "file_manifest.json")

class RunRequest(BaseModel):
    dataset: str  # e.g. "mirror-merged/2026-04-29"
    file_list: list[str] = None

@app.get("/api/training/file-list")
def get_file_list():
    list_path = os.path.join(os.path.dirname(__FILE_MANIFEST_PATH__), "training", "file_list.json")
    if not os.path.exists(list_path):
        raise HTTPException(status_code=404, detail="file_list.json not found")
    with open(list_path) as f:
        return json.load(f)

@app.post("/api/training/run")
def run_training(req: RunRequest):
    if not os.path.exists(MANIFEST_PATH):
        raise HTTPException(status_code=404, detail="Manifest not found. Run prefetch first.")
    # Launch training via Lightning reuse script
    result = subprocess.run(
        ["bash", "scripts/lightning_reuse.sh", "surrogate-train", "training/train.py"],
        capture_output=True, text=True, cwd=os.path.dirname(__file__) + "/.."
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Launch failed: {result.stderr}")
    return {"status": "submitted", "output": result.stdout}
```

---

### 3) CDN-only IterableDataset (training/train.py)
- Uses manifest CDN URLs; no HF API during training.  
- Lightweight projection to `prompt`/`response`.

```python
# training/train.py
import json, os, io, requests, pyarrow.parquet as pq
from torch.utils.data import IterableDataset

MANIFEST = os.path.join(os.path.dirname(__file__), "file_manifest.json")

class CDNParquetIterable(IterableDataset):
    def __init__(self, start=0, end=None):
        with open(MANIFEST) as f:
            self.files = json.load(f)[start:end]
        self.urls = [f["cdn_url"] for f in self.files]

    def _stream_file(self, url):
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        tbl = pq.read_table(io.BytesIO(resp.content))
        prompt_col = tbl.column("prompt") if "prompt" in tbl.column_names else None
        response_col = tbl.column("response") if "response" in tbl.column_names else None
        for i in range(tbl.num_rows):
            prompt = prompt_col[i].as_py() if prompt_col is not None else ""
            response = response_col[i].as_py() if response_col is not None else ""
            if prompt and response:
                yield {"prompt": prompt, "response": response}

    def __iter__(self):
        for url in self.urls:
            try:
                yield from self._stream_file(url)
            except Exception as e:
                print(f"Skipping {url}: {e}")
                continue
```

---

### 4) Lightning Studio reuse + idle-stop guard (bash wrapper)
- Reuse running studio; start if stopped; avoid duplicates.

```bash
# scripts/lightning_reuse.sh
#!/usr/bin/env bash
set -euo pipefail

STUDIO_NAME="${1:-surrogate-train}"
SCRIPT="${2:-training/train.py}"

python3 - <<PY
import asyncio, os, sys
from lightning_sdk import Teamspace, Studio, Machine

TEAMSPACE = Teamspace.load("default")

def find_running(name):
    for s in TEAMSPACE.studios:
        if s.name == name and s.status == "Running":
            return s
    return None

studio = find_running("${STUDIO_NAME}")
if studio is None:
    print("Starting new studio...")
    studio = Studio.create(
        name="${STUDIO_NAME}",
        machine=Machine.L40S,
        teamspace="default",
        create_ok=True,
    )
