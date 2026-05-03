# airship / frontend

## Highest-Value Incremental Improvement (≤2h)

**What**: Add a CDN-first training slice to Surrogate frontend that:
1. Exposes a small UI + API to trigger “list one date folder from HF” (Mac orchestration) → saves `file-list.json`
2. Embeds that list in the training script so Lightning Studio does **CDN-only fetches** (zero HF API calls during data load)
3. Projects each file to `{prompt, response}` only at parse time (strict schema)
4. Reuses a running Lightning Studio to avoid idle-timeout quota waste

**Why**: Eliminates HF API 429s during Surrogate training and prevents Lightning idle-timeout quota waste; ships in <2h with minimal surface area.

---

## Implementation Plan

1. **Backend (FastAPI)** — `surrogate/api/training_cdn.py`
   - `POST /training/list-hf-folder` — calls HF `list_repo_tree(path, recursive=False)` for a single date folder, returns file list (no recursive pagination).
   - `POST /training/save-file-list` — persists `file-list.json` to surrogate workspace.
   - `GET /training/file-list` — returns current list.
   - `POST /training/launch-studio` — reuses running Lightning Studio or starts one (L40S priority, fallback to free-tier clouds), passes `file-list.json` and training script entrypoint.

2. **Frontend (React)** — `surrogate/src/pages/TrainingCDNPage.tsx`
   - Date picker + repo/folder input.
   - “List HF folder” → shows file count.
   - “Save file list” → persists.
   - “Launch Studio” → shows studio status, reconnect button, logs tail.
   - “Project & Train” — runs local projection script that streams CDN files and yields `{prompt, response}` parquet for upload.

3. **Training script (Lightning)** — `surrogate/training/train_cdn.py`
   - Loads `file-list.json`.
   - Uses `hfcdn_fetch()` (no auth) to stream files via `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.
   - Projects each file to `{prompt, response}`; writes to `batches/mirror-merged/{date}/{slug}.parquet` (no extra cols).
   - Uses Lightning `Studio` reuse pattern; checks status and restarts if idle-stopped.

4. **Utilities** — `surrogate/utils/hfcdn.py`
   - `list_hf_folder(repo, folder)` — single tree call.
   - `hfcdn_fetch_urls(file_list)` — returns CDN URLs.
   - `project_to_pair(raw)` — per-file projection (supports jsonl/parquet/text).

5. **Deployment** — ensure `surrogate` service exposes new endpoints; no infra changes.

---

## Code Snippets

### Backend: `surrogate/api/training_cdn.py`
```python
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from huggingface_hub import HfApi, list_repo_tree
import requests

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
FILE_LIST_PATH = BASE_DIR / "training" / "file-list.json"

HF_API = HfApi()

@router.post("/list-hf-folder")
def list_hf_folder(repo: str, folder: str):
    """
    List a single date folder (non-recursive) to avoid pagination/429.
    Returns: { "files": ["file1.jsonl", ...] }
    """
    try:
        tree = list_repo_tree(repo_id=repo, path=folder, recursive=False)
        files = [t.rfilename for t in tree if not t.rfilename.endswith("/")]
        return {"files": files}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HF list failed: {exc}") from exc

@router.post("/save-file-list")
def save_file_list(repo: str, folder: str, files: list[str]):
    payload = {"repo": repo, "folder": folder, "files": files}
    FILE_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    FILE_LIST_PATH.write_text(json.dumps(payload, indent=2))
    return {"saved": str(FILE_LIST_PATH), "count": len(files)}

@router.get("/file-list")
def get_file_list():
    if not FILE_LIST_PATH.exists():
        raise HTTPException(status_code=404, detail="No file list saved")
    return json.loads(FILE_LIST_PATH.read_text())

@router.post("/launch-studio")
def launch_studio(machine: str = "lightning-ai/L40S-0.5A"):
    from lightning import Studio, Teamspace
    # Reuse running studio if exists
    for s in Teamspace().studios:
        if s.name == "surrogate-training" and s.status == "Running":
            return {"reused": True, "studio_id": s.id, "status": s.status}
    # Start new
    studio = Studio(
        name="surrogate-training",
        cloud=machine,
        cluster=None,
        create_ok=True,
    )
    return {"reused": False, "studio_id": studio.id, "status": studio.status}
```

### Utility: `surrogate/utils/hfcdn.py`
```python
from pathlib import Path
import json
import requests
import pyarrow.parquet as pq
import pyarrow as pa
import pandas as pd

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_fetch_urls(repo: str, files: list[str]):
    for f in files:
        yield f, f"{CDN_ROOT}/{repo}/resolve/main/{f}"

def hfcdn_fetch(repo: str, files: list[str], chunk_size: int = 8192):
    for fname, url in cdn_fetch_urls(repo, files):
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        yield fname, resp.content

def project_to_pair(fname: str, content: bytes):
    suffix = Path(fname).suffix.lower()
    if suffix == ".jsonl":
        lines = [json.loads(l) for l in content.decode().strip().splitlines() if l.strip()]
        # flexible: accept {prompt,response} or {input,output} or {instruction,completion}
        pairs = []
        for obj in lines:
            prompt = obj.get("prompt") or obj.get("input") or obj.get("instruction") or ""
            response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
            if prompt and response:
                pairs.append({"prompt": prompt, "response": response})
        return pairs
    elif suffix == ".parquet":
        tbl = pq.read_table(pa.BufferReader(content))
        df = tbl.to_pandas()
        # same flexible column mapping
        map_ = {"prompt": ["prompt", "input", "instruction"], "response": ["response", "output", "completion"]}
        prompt_col = next((c for c in map_["prompt"] if c in df.columns), None)
        response_col = next((c for c in map_["response"] if c in df.columns), None)
        if prompt_col and response_col:
            return df[[prompt_col, response_col]].rename(columns={prompt_col: "prompt", response_col: "response"}).to_dict(orient="records")
        return []
    else:
        # naive text split by double newline as separator; best-effort
        text = content.decode(errors="replace")
        parts = [p.strip() for p in text.split("\n\n") if p.strip()]
        # assume alternating prompt/response if even count, else skip
        if len(parts) % 2 == 0:
            return [{"prompt": parts[i], "response": parts[i + 1]} for i in range(0, len(parts), 2)]
        return []

def write_mirror_parquet(pairs, repo: str, folder: str, out_root: Path):
    if not pairs:
        return None
    date_part = Path(folder).name
    out_dir = out_root / "batches" / "mirror-merged" / date_part
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = repo.replace("/", "_")
    out_path = out_dir / f"{slug}.parquet"
    df = pd.DataFrame(pairs)
    tbl = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(tbl, out_path)
    return str(out_path)
```

### Training script (Lightning
