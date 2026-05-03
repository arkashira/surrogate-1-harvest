# airship / frontend

## Final Synthesized Answer (Best Parts + Correctness + Actionability)

**Highest-Value Incremental Improvement**  
Add an **HF CDN-bypass dataset loader with Lightning Studio reuse** to the training UI.  
- Eliminates HF API 429s and `pyarrow.CastError` during dataset loading.  
- Reduces Lightning quota burn by reusing running Studios.  
- Ships in ≤2 hours with minimal frontend changes and one backend helper.

---

## Corrected & Consolidated Implementation Plan (≤2h)

| Step | Owner | Time | Concrete Deliverable |
|------|-------|------|----------------------|
| 1 | FE | 20m | Add “Use CDN-bypass” toggle and “Reuse Studio” checkbox to training form; add error boundary for HF failures. |
| 2 | BE | 20m | Implement `/api/training/prepare` (single `list_repo_tree` → JSON; includes CDN URLs; 1h cache). |
| 3 | FE | 25m | Wire training form to call `/api/training/prepare`; show “Reuse active” badge; preflight CDN reachability check. |
| 4 | BE | 25m | Add `/api/training/start-cdn` that launches Lightning job with CDN-only loader; reuses running Studio or creates one. |
| 5 | FE | 20m | Update training UI to poll Studio status; display loader type and fallback behavior. |
| 6 | QA | 30m | Smoke test with small HF dataset; verify zero HF API calls during training load; test rollback. |

---

## Corrected & Actionable Code Snippets

### 1) Frontend: training form additions  
`/opt/axentx/airship/frontend/src/components/TrainingForm.tsx`
```tsx
<div className="flex flex-col gap-3 border-t pt-3 mt-3">
  <label className="flex items-center gap-2 cursor-pointer">
    <input
      type="checkbox"
      name="cdnBypass"
      checked={form.cdnBypass}
      onChange={(e) => setForm({ ...form, cdnBypass: e.target.checked })}
    />
    <span className="text-sm">Use CDN-bypass (avoid HF API 429s)</span>
  </label>

  <label className="flex items-center gap-2 cursor-pointer">
    <input
      type="checkbox"
      name="reuseStudio"
      checked={form.reuseStudio}
      onChange={(e) => setForm({ ...form, reuseStudio: e.target.checked })}
    />
    <span className="text-sm">Reuse running Studio</span>
  </label>
</div>
```

### 2) Frontend: prepare hook with error boundary  
`frontend/src/hooks/useTrainingPrepare.ts`
```ts
export async function prepareTraining(opts: {
  repo: string;
  folder: string;
  cdnBypass: boolean;
  reuseStudio: boolean;
}) {
  const res = await fetch('/api/training/prepare', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`Prepare failed: ${err}`);
  }
  return res.json(); // { fileList: Array<{path:string,cdn_url:string}>, studio?: {name:string,status:string} }
}
```

### 3) Backend: `/api/training/prepare` (cached, non-recursive)  
`/opt/axentx/airship/surrogate/api/training.py`
```python
from fastapi import APIRouter, HTTPException
from huggingface_hub import HfApi
import time
from datetime import datetime, timezone

router = APIRouter()
_hf_api = HfApi()
_CACHE_TTL = 3600  # 1h
_CACHE = {}

def _cdn_url(repo: str, path: str) -> str:
    # Correct: CDN URL must resolve raw file without auth
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

@router.post("/prepare")
async def prepare_training(payload: dict):
    repo = payload.get("repo")
    folder = payload.get("folder", "")
    reuse = bool(payload.get("reuseStudio", True))

    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")

    cache_key = f"{repo}:{folder}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    try:
        # Non-recursive per folder to avoid pagination and 429s
        tree = _hf_api.list_repo_tree(repo_id=repo, path=folder or None, recursive=False)
        files = [
            {
                "path": f.rfilename,
                "cdn_url": _cdn_url(repo, f.rfilename),
                "size": getattr(f, "size", None),
            }
            for f in tree
            if not f.rfilename.endswith("/") and f.rfilename.endswith(".parquet")
        ]
        result = {
            "repo": repo,
            "path": folder,
            "fileList": files,
            "generated": datetime.now(timezone.utc).isoformat(),
            "studio": None,
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HF repo read failed: {exc}")

    if reuse:
        try:
            from lightning import Teamspace
            for s in Teamspace().studios:
                if s.name == "surrogate-training" and s.status == "running":
                    result["studio"] = {"name": s.name, "status": s.status}
                    break
        except Exception:
            # Don't fail prepare if Studio introspection fails
            pass

    _CACHE[cache_key] = (result, now)
    return result
```

### 4) CDN-only parquet streamer (correct projection + robust fetch)  
`surrogate/training/cdn_loader.py`
```python
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from typing import Iterator, Dict, Any

def cdn_parquet_stream(repo: str, files_info: list[Dict[str, str]], batch_size: int = 64) -> Iterator[Dict[str, Any]]:
    base = f"https://huggingface.co/datasets/{repo}/resolve/main"
    for info in files_info:
        path = info["path"]
        url = f"{base}/{path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        table = pq.read_table(pa.BufferReader(resp.content))
        # Project only required columns to avoid CastError
        available = set(table.column_names)
        to_select = [c for c in ["prompt", "response"] if c in available]
        if not to_select:
            continue
        table = table.select(to_select)
        for batch in table.to_batches(max_chunksize=batch_size):
            yield from batch.to_pylist()
```

### 5) Lightning Studio reuse + launch (correct flow)  
`surrogate/training/launch.py`
```python
from lightning import Studio, Machine
import json

def launch_cdn_training(file_list_info: list, reuse: bool = True) -> str:
    studio = None
    if reuse:
        try:
            for s in Studio.list():
                if s.name == "surrogate-training" and s.status == "running":
                    studio = s
                    break
        except Exception:
            studio = None

    if studio is None:
        studio = Studio.create(
            name="surrogate-training",
            machine=Machine.L40S,
            python_version="3.10",
        )
    elif studio.status != "running":
        studio.start(machine=Machine.L40S)

    # Pass minimal metadata to training script
    meta = {
        "files": file_list_info,
        "use_cdn": True,
    }
    studio.run(
        [
            "python",
            "train.py",
            "--meta",
            json.dumps(meta),
        ],
        sync=False,
    )
    return studio.name
```

---

## Verification Checklist (Corrected)
- [ ] HF API calls during training load = 0 (only
