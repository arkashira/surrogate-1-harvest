# airship / frontend

## Highest-Value Incremental Improvement (≤2h)

**What**: Add a CDN-first training slice to Surrogate that:
1. Exposes a tiny backend API + frontend UI to trigger “list one HF date-folder” (Mac orchestration) → saves `file-list.json`
2. Uses `file-list.json` in Lightning training to fetch via CDN-only (zero API calls during training)
3. Reuses a running Lightning Studio to avoid quota churn

**Why**: Directly addresses the HF CDN bypass and Lightning quota patterns; unblocks fast iteration on training data ingestion without hitting HF API rate limits or recreating studios.

---

## Implementation Plan

### 1) Backend (Surrogate — port 8001)
- Add endpoint `POST /api/training/list-hf-folder`  
  - Input: `{ repo, path, revision? }`  
  - Calls HF `list_repo_tree(path, recursive=False)` once (from backend, not browser)  
  - Saves `training/file-list.json` (repo-root-relative) with `{ repo, path, revision, files[], fetchedAt }`
- Add endpoint `GET /api/training/file-list` → returns saved `file-list.json`
- Add endpoint `POST /api/training/start-lightning`  
  - Reads `file-list.json`, builds CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{file}`)  
  - Reuses running Lightning Studio or starts L40S if stopped  
  - Runs training script with file-list embedded (no HF API calls during data loading)

### 2) Frontend (Arkship UI — port 3000)
- Add page `/training` with:
  - Form: Repo / Path / Revision → “List HF Folder”
  - Status area: last file-list, fetchedAt, file count
  - Button: “Start Lightning Training” (disabled if no file-list)
  - Logs pane: streams studio status / training start events

### 3) Lightning training script (surrogate-side)
- Accept file-list JSON at launch (env var or mounted file)
- Dataset loader uses `wget`/`requests` against CDN URLs only (no `load_dataset` with HF API)
- Project to `{prompt,response}` at parse time; write parquet to `batches/mirror-merged/{date}/{slug}.parquet`
- On studio stop, mark status; orchestrator can restart with same file-list

### 4) Security / ops
- HF token stored in backend env (not frontend)
- Rate-limit: single `list_repo_tree` call per folder; cache result for 5min to avoid repeated hits
- Studio reuse: list Teamspace studios, pick running one with matching name; fallback to `L40S` in `lightning-public-prod`

---

## Code Snippets

### Backend: HF folder listing + file-list persistence
```python
# surrogate/api/training.py
import os, json, datetime
from fastapi import APIRouter, HTTPException
from huggingface_hub import HfApi

router = APIRouter()
HF_API = HfApi()
FILE_LIST_PATH = os.path.join(os.path.dirname(__file__), "..", "file-list.json")

@router.post("/list-hf-folder")
def list_hf_folder(repo: str, path: str = "", revision: str = "main"):
    try:
        tree = HF_API.list_repo_tree(repo=repo, path=path or "", revision=revision, recursive=False)
        files = [f.rfilename for f in tree if f.type == "file"]
        payload = {
            "repo": repo,
            "path": path,
            "revision": revision,
            "files": files,
            "fetchedAt": datetime.datetime.utcnow().isoformat() + "Z"
        }
        with open(FILE_LIST_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        return {"ok": True, "count": len(files), "fileList": payload}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HF list failed: {str(e)}")

@router.get("/file-list")
def get_file_list():
    if not os.path.exists(FILE_LIST_PATH):
        raise HTTPException(status_code=404, detail="No file-list saved")
    with open(FILE_LIST_PATH) as f:
        return json.load(f)
```

### Backend: Lightning orchestration (reuse studio)
```python
# surrogate/api/lightning.py
import os
from fastapi import APIRouter
from lightning.app import Lightning, Machine
from lightning.app.utilities.cloud import _get_project
from lightning.app.utilities.exceptions import NotFoundException

router = APIRouter()
LIGHTNING_APP_NAME = os.getenv("LIGHTNING_APP_NAME", "surrogate-training")

def find_running_studio():
    cloud = _get_project()
    for studio in cloud.studios:
        if studio.name == LIGHTNING_APP_NAME and studio.status == "running":
            return studio
    return None

@router.post("/start-lightning")
def start_lightning():
    studio = find_running_studio()
    if studio:
        return {"ok": True, "reused": True, "studioId": studio.id, "status": studio.status}

    # Start new (or stopped) studio
    lightning = Lightning()
    machine = Machine.L40S  # free tier fallback handled by Lightning
    app = lightning.create_app(
        name=LIGHTNING_APP_NAME,
        target="surrogate.training.train:main",  # expects file-list.json present
        machine=machine,
        create_ok=True
    )
    return {"ok": True, "reused": False, "appId": app.id, "status": "starting"}
```

### Frontend: Training page (Arkship)
```tsx
// arkship/src/pages/Training.tsx
import { useState } from "react";
import axios from "axios";

export default function TrainingPage() {
  const [repo, setRepo] = useState("org/surrogate-data");
  const [path, setPath] = useState("batches/2026-05-03");
  const [revision, setRevision] = useState("main");
  const [fileList, setFileList] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);

  const listFolder = async () => {
    setLoading(true);
    try {
      const res = await axios.post("http://localhost:8001/api/training/list-hf-folder", { repo, path, revision });
      setFileList(res.data.fileList);
      setLogs((l) => [...l, `Listed ${res.data.count} files from ${repo}/${path}`]);
    } catch (e: any) {
      setLogs((l) => [...l, `Error: ${e.response?.data?.detail || e.message}`]);
    } finally {
      setLoading(false);
    }
  };

  const startLightning = async () => {
    setLoading(true);
    try {
      const res = await axios.post("http://localhost:8001/api/training/start-lightning");
      setLogs((l) => [...l, `Lightning ${res.data.reused ? "reused" : "started"} (id: ${res.data.appId || res.data.studioId})`]);
    } catch (e: any) {
      setLogs((l) => [...l, `Lightning start failed: ${e.response?.data?.detail || e.message}`]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <h2>CDN-First Training Slice</h2>
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <input placeholder="repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
        <input placeholder="path" value={path} onChange={(e) => setPath(e.target.value)} />
        <input placeholder="revision" value={revision} onChange={(e) => setRevision(e.target.value)} />
        <button onClick={listFolder} disabled={loading}>List HF Folder</button>
        <button onClick={startLightning} disabled={loading || !fileList}>Start Lightning Training</button>
      </div>

      {fileList && (
        <div style={{ marginBottom: 16 }}>
          <strong>Last file-list:</strong> {fileList.fetchedAt
