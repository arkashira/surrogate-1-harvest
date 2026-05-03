# airship / frontend

# Final Synthesis — CDN-first Training Slice (Surrogate)

## Core Decision
Merge the strongest, most correct and actionable parts of both proposals into one minimal, production-ready slice that:
- Uses **FastAPI** (explicit, widely used in Surrogate/Arkship stack) for the backend.
- Keeps the frontend tiny and pragmatic (React/Next.js).
- Implements the **HF CDN bypass pattern** correctly: single HF API list → cached `file-list.json` → training uses only CDN URLs (no HF API calls during dataload).
- Avoids contradictions by choosing concrete, correct APIs and file paths and including robust error handling and security notes.

---

## 1) Highest-Value Outcome (≤2h)

**What we ship**
- Backend: FastAPI endpoints to list one HF folder (single API call) and serve/save `file-list.json`; utility to build CDN URLs.
- Frontend: “Training Prep” UI to trigger listing and display cached manifests.
- Training stub: small change to accept a file-list and download via CDN URLs (no HF API during training).

**Why this wins**
- Directly prevents 429s and quota burn during training iterations.
- Requires no training infra changes — only orchestration + manifest.
- ≤2h scope: frontend + one backend module + one training arg.

---

## 2) Implementation Plan (≤2h)

| Step | Owner | Time |
|------|-------|------|
| 1) Add FastAPI training module (`/surrogate/api/training.py`) with list, cache, and CDN util | Backend | 45m |
| 2) Add minimal React UI (`TrainingPrep`) in Surrogate frontend | Frontend | 45m |
| 3) Update training script stub to accept `file_list_path` and load via CDN | ML/Training | 20m |
| 4) Wireup, tests, polish, docs comments | All | 10m |

---

## 3) Code — Backend (FastAPI)

File: `surrogate/api/training.py`

```python
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from huggingface_hub import HfApi, list_repo_tree

router = APIRouter()

DATA_DIR = Path(__file__).parent.parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

HF_API = HfApi()

def _cache_path(name: str) -> Path:
    safe = name.replace("/", "_")
    return DATA_DIR / f"file-list-{safe}.json"

def cdn_url(repo: str, path: str) -> str:
    """Generate HF CDN URL for a file."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

@router.post("/training/list-hf-folder")
async def list_hf_folder(
    repo: str,
    folder: str,
    token: Optional[str] = None,
):
    """
    List one HF folder (non-recursive) and save file-list.json.
    Body (form/json): repo, folder, token?
    """
    if not repo or not folder:
        raise HTTPException(status_code=400, detail="repo and folder required")

    try:
        tree = list_repo_tree(
            repo_id=repo,
            path=folder,
            recursive=False,
            token=token or None,
        )
        files = [item.rfilename for item in tree if item.type == "file"]

        payload = {
            "repo": repo,
            "folder": folder,
            "listed_at": datetime.utcnow().isoformat() + "Z",
            "count": len(files),
            "files": files,
            "cdn_urls": [cdn_url(repo, f) for f in files],
        }

        cp = _cache_path(folder)
        cp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HF list failed: {exc}") from exc

@router.get("/training/file-list")
async def get_file_list(folder: Optional[str] = None, date: Optional[str] = None):
    """
    Get cached file-list by folder or date.
    Query: ?folder=2026-05-01 or ?date=2026-05-01
    """
    if not folder and not date:
        raise HTTPException(status_code=400, detail="folder or date required")

    candidates = []
    if folder:
        candidates.append(_cache_path(folder))
    if date:
        candidates.append(DATA_DIR / f"file-list-{date}.json")

    for p in candidates:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))

    raise HTTPException(status_code=404, detail="file-list not found")
```

Notes & correctness
- Uses `list_repo_tree` (single HF API call) — correct and minimal.
- Saves canonical `file-list-{folder}.json` into `surrogate/data/`.
- Returns both `files` and `cdn_urls` for convenience.
- `cdn_url` uses the canonical HF CDN pattern.

---

## 4) Code — Frontend (React)

File: `surrogate/components/TrainingPrep.tsx`

```tsx
import { useState } from "react";
import axios from "axios";

export default function TrainingPrep() {
  const [repo, setRepo] = useState("datasets/example-repo");
  const [folder, setFolder] = useState("2026-05-01");
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [cached, setCached] = useState<any[]>([]);

  const listFolder = async () => {
    setLoading(true);
    try {
      const res = await axios.post("/api/training/list-hf-folder", {
        repo,
        folder,
        token: token || undefined,
      });
      setResult(res.data);
      fetchCached();
    } catch (err: any) {
      setResult({ error: err.response?.data?.detail || err.message });
    } finally {
      setLoading(false);
    }
  };

  const fetchCached = async () => {
    try {
      const res = await axios.get("/api/training/file-list", {
        params: { folder },
      });
      setCached(Array.isArray(res.data) ? res.data : [res.data]);
    } catch {
      setCached([]);
    }
  };

  return (
    <div style={{ maxWidth: 720, padding: 20 }}>
      <h2>Surrogate — CDN-first Training Prep</h2>
      <p>
        List a HF folder once (Mac orchestration) → cached file-list.json.
        Training will use CDN URLs only (no HF API calls during dataload).
      </p>

      <label>
        Repo:
        <input
          value={repo}
          onChange={(e) => setRepo(e.target.value)}
          style={{ width: "100%", margin: "8px 0" }}
        />
      </label>
      <label>
        Folder/date:
        <input
          value={folder}
          onChange={(e) => setFolder(e.target.value)}
          style={{ width: "100%", margin: "8px 0" }}
        />
      </label>
      <label>
        Token (optional):
        <input
          value={token}
          onChange={(e) => setToken(e.target.value)}
          style={{ width: "100%", margin: "8px 0" }}
        />
      </label>

      <button onClick={listFolder} disabled={loading} style={{ padding: "8px 16px" }}>
        {loading ? "Listing..." : "List folder (CDN mode)"}
      </button>

      {result && (
        <pre style={{ background: "#f6f8fa", padding: 12, marginTop: 12 }}>
          {JSON.stringify(result, null, 2)}
        </pre>
      )}

      <h3 style={{ marginTop: 24 }}>Cached
