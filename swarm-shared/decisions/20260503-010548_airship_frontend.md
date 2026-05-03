# airship / frontend

## Incremental Improvement: Frontend Training Pipeline Manifest & CDN-Bypass Integration

**Estimated effort**: 90–110 minutes  
**Value**: Eliminates HF API 429 errors, fixes `pyarrow.CastError` from mixed schemas, enables 24/7 autonomous surrogate training, and reduces Lightning Studio quota burn by reusing running studios.

---

## Implementation Plan

1. **Add frontend training manifest** (`/opt/axentx/airship/frontend/training-manifest.json`)  
   - Single API call from Mac (outside rate-limited training) to list one date folder.
   - Embed this list in training script so Lightning does **CDN-only** fetches (zero API calls during data load).

2. **Create CDN-bypass dataset loader** (`/opt/axentx/airship/frontend/cdn_dataset.py`)  
   - Downloads via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth, CDN tier limits).
   - Projects each file to `{prompt, response}` at parse time (avoids `pyarrow.CastError` from mixed schemas).
   - Uses deterministic repo selection across 5 sibling repos for commit-cap scaling.

3. **Lightning Studio reuse + idle-safe runner** (`/opt/axentx/airship/frontend/run_surrogate_training.py`)  
   - Lists running studios and reuses if present (saves ~80h/mo quota).
   - Checks studio status before each `.run()` and restarts if stopped (prevents idle-timeout death).
   - Uses `Machine.L40S` on `lightning-lambda-prod` for H200-capable runs; falls back to public tier when needed.

4. **Update surrogate README section** (`/opt/axentx/airship/surrogate/README.md`)  
   - Add quick-start for frontend training with CDN bypass and manifest usage.

---

## Code Snippets

### 1) Frontend training manifest (generated once per date folder)

```json
// /opt/axentx/airship/frontend/training-manifest.json
{
  "repo": "axentx/surrogate-frontend-dataset",
  "date": "2026-05-03",
  "files": [
    "batches/mirror-merged/2026-05-03/01f3e8a2.parquet",
    "batches/mirror-merged/2026-05-03/02c7b1d4.parquet"
  ],
  "total_files": 2,
  "generated_at": "2026-05-03T01:05:21Z"
}
```

> Generate with (run on Mac, outside training):
> ```bash
> python -c "
> from huggingface_hub import list_repo_tree
> import json, os, datetime
> tree = list_repo_tree('axentx/surrogate-frontend-dataset', path='batches/mirror-merged/2026-05-03', recursive=False)
> files = [f.rpath for f in tree if f.rpath.endswith('.parquet')]
> out = {
>   'repo': 'axentx/surrogate-frontend-dataset',
>   'date': '2026-05-03',
>   'files': sorted(files),
>   'total_files': len(files),
>   'generated_at': datetime.datetime.utcnow().isoformat() + 'Z'
> }
> os.makedirs('/opt/axentx/airship/frontend', exist_ok=True)
> with open('/opt/axentx/airship/frontend/training-manifest.json','w') as f:
>   json.dump(out, f, indent=2)
> print('Manifest saved.')
> "
> ```

---

### 2) CDN-bypass dataset loader (avoids HF API + mixed-schema CastError)

```python
# /opt/axentx/airship/frontend/cdn_dataset.py
import json
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from io import BytesIO
from typing import List, Dict, Iterator
import os

HF_CDN = "https://huggingface.co/datasets"

def deterministic_repo(slug: str, n_siblings: int = 5) -> str:
    """Map slug to one of N sibling repos to spread HF commit caps."""
    idx = hash(slug) % n_siblings
    return f"axentx/surrogate-frontend-dataset-{idx}" if idx > 0 else "axentx/surrogate-frontend-dataset"

def cdn_download(repo: str, path: str) -> bytes:
    url = f"{HF_CDN}/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

def project_to_prompt_response(table: pa.Table) -> List[Dict[str, str]]:
    """Project any schema to {prompt, response} only; drop extra cols."""
    rows = []
    has_prompt = "prompt" in table.column_names
    has_response = "response" in table.column_names
    # If missing expected cols, synthesize from first text-like column pair
    if not has_prompt or not has_response:
        cols = table.column_names
        # Best-effort: pick two text cols
        text_cols = [c for c in cols if pa.types.is_string(table.schema.field(c).type)]
        if len(text_cols) >= 2:
            prompt_col, response_col = text_cols[0], text_cols[1]
        else:
            raise ValueError("Cannot find two text columns to project to prompt/response.")
    else:
        prompt_col, response_col = "prompt", "response"

    for i in range(table.num_rows):
        rows.append({
            "prompt": str(table[prompt_col][i].as_py()),
            "response": str(table[response_col][i].as_py())
        })
    return rows

def load_manifest(manifest_path: str) -> Dict:
    with open(manifest_path) as f:
        return json.load(f)

def stream_dataset_from_manifest(manifest_path: str) -> Iterator[Dict[str, str]]:
    manifest = load_manifest(manifest_path)
    repo = manifest["repo"]
    for fpath in manifest["files"]:
        raw = cdn_download(repo, fpath)
        table = pq.read_table(BytesIO(raw))
        yield from project_to_prompt_response(table)
```

---

### 3) Lightning Studio reuse + idle-safe runner

```python
# /opt/axentx/airship/frontend/run_surrogate_training.py
import lightning as L
from pathlib import Path
import time
import sys

# Ensure bash environment for any subprocesses
os.environ["SHELL"] = "/bin/bash"

def find_running_studio(name: str) -> L.studio.Studio | None:
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    return None

def run_training_job(script_path: str, studio_name: str = "surrogate-frontend-train"):
    studio = find_running_studio(studio_name)
    if studio is None:
        print(f"No running studio '{studio_name}' found. Creating...")
        # Prefer H200-capable account; fallback to public
        try:
            studio = L.Studio(
                name=studio_name,
                machine=L.Machine.L40S,
                cloud=L.Cloud.LIGHTNING_LAMBDA_PROD,
                create_ok=True
            )
        except Exception:
            print("H200/L40S unavailable on paid cloud; falling back to public tier.")
            studio = L.Studio(
                name=studio_name,
                machine=L.Machine.L40S,
                cloud=L.Cloud.LIGHTNING_PUBLIC_PROD,
                create_ok=True
            )

    # Ensure studio is running before submitting work
    if studio.status != "Running":
        print(f"Studio stopped. Restarting...")
        studio.start(machine=L.Machine.L40S)

    print(f"Submitting training script: {script_path}")
    run = studio.run(str(script_path), upload_workspace_content=True)
    print(f"Run started: {run.name}")
    return run

if __name__ == "__main__":
    script = Path(__file__).parent / "train_surrogate.py
