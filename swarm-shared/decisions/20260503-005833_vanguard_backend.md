# vanguard / backend

### Diagnosis (merged)
- No persisted `(repo, dateFolder)` manifest → every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Recursive enumeration + pagination amplifies rate-limit pressure and exposes mixed-schema files; wastes I/O.
- Training likely uses `load_dataset(streaming=True)` on heterogeneous repo → `pyarrow.CastError` at runtime.
- Lightning Studio idle-stop kills training; no reuse logic recreates studio and burns quota.
- No CDN-only data path → authenticated API calls continue during Lightning data loading.

### Proposed change (merged)
Add a lightweight manifest generator + Lightning launcher that:
- Persists `manifests/{repo}/{dateFolder}.json` listing only file paths (single non-recursive `list_repo_tree` per date folder).
- Embeds manifest in `train.py` so Lightning training uses CDN-only fetches (`https://huggingface.co/datasets/.../resolve/main/...`) with zero authenticated API calls.
- Reuses a running Lightning studio when available (`vanguard-train-{repo_slug}-{dateFolder}`) and restarts it if stopped/idle-killed.
- Projects to `{prompt,response}` at parse time (schema-agnostic) and writes attribution into filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`).

Scope:
- `/opt/axentx/vanguard/backend/manifest.py` (new)
- `/opt/axentx/vanguard/backend/train.py` (modify)
- `/opt/axentx/vanguard/backend/launch_train.py` (new)

---

### Implementation

#### 1) Manifest generator (non-recursive, per date folder)
```python
# /opt/axentx/vanguard/backend/manifest.py
import json
import os
from datetime import datetime
from pathlib import Path
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/mirror-merged")
MANIFEST_ROOT = Path(__file__).parent.parent / "manifests"

def list_date_folder(date_folder: str, repo: str = HF_REPO) -> list[str]:
    """Single non-recursive tree call for one date folder; returns file paths."""
    api = HfApi()
    items = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    # Keep only files (FileInfo.rfilename)
    files = sorted([it.rfilename for it in items if not it.rfilename.endswith("/")])
    return files

def build_manifest(date_folder: str, repo: str = HF_REPO, out_dir: Path | None = None) -> Path:
    out_dir = Path(out_dir or MANIFEST_ROOT / repo.replace("/", "_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    files = list_date_folder(date_folder, repo)
    manifest = {
        "repo": repo,
        "dateFolder": date_folder,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "version": 1
    }
    path = out_dir / f"{date_folder}.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: manifest.py <dateFolder> [repo]")
        sys.exit(1)
    df = sys.argv[1]
    repo = sys.argv[2] if len(sys.argv) > 2 else HF_REPO
    p = build_manifest(df, repo)
    print(f"Manifest written: {p}")
```

#### 2) Modified training script (CDN-only, schema-agnostic projection)
```python
# /opt/axentx/vanguard/backend/train.py
import json
import os
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path
from typing import Iterator, Dict, Any

MANIFEST_ROOT = Path(__file__).parent.parent / "manifests"
HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/mirror-merged")

def load_manifest(date_folder: str, repo: str = HF_DATASET_REPO) -> dict:
    repo_key = repo.replace("/", "_")
    manifest_path = MANIFEST_ROOT / repo_key / f"{date_folder}.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text())

def cdn_url(repo: str, file_path: str) -> str:
    # CDN-only; no authenticated API calls during training
    return f"https://huggingface.co/{repo}/resolve/main/{file_path}"

def project_to_prompt_response(batch: Dict[str, Any]) -> Iterator[Dict[str, str]]:
    """Schema-agnostic projection to {prompt, response}."""
    prompts = batch.get("prompt") or batch.get("input") or batch.get("text")
    responses = batch.get("response") or batch.get("output") or batch.get("completion")
    if prompts is None or responses is None:
        # fallback: try to extract from raw dict rows
        for row in batch if isinstance(batch, list) else [batch]:
            p = row.get("prompt") or row.get("input") or row.get("text")
            r = row.get("response") or row.get("output") or row.get("completion")
            if p is not None and r is not None:
                yield {"prompt": str(p), "response": str(r)}
        return
    # vectorized-like handling
    if isinstance(prompts, list) and isinstance(responses, list):
        for p, r in zip(prompts, responses):
            if p is not None and r is not None:
                yield {"prompt": str(p), "response": str(r)}
    else:
        if prompts is not None and responses is not None:
            yield {"prompt": str(prompts), "response": str(responses)}

def load_and_project(date_folder: str, repo: str = HF_DATASET_REPO) -> Iterator[Dict[str, str]]:
    manifest = load_manifest(date_folder, repo)
    for file_rel in manifest.get("files", []):
        url = cdn_url(repo, file_rel)
        # Lightweight: stream remote parquet via pyarrow.dataset or fsspec+pyarrow
        try:
            import pyarrow.dataset as ds
            dataset = ds.dataset(url, format="parquet")
            for batch in dataset.to_batches():
                # Convert batch to dict-of-arrays for projection
                cols = batch.schema.names
                data = {c: batch.column(c).to_pylist() for c in cols}
                for row in project_to_prompt_response(data):
                    yield row
        except Exception as exc:
            # If CDN parquet fails, skip file but do not crash training
            print(f"Skipping {url} due to {exc}")
            continue

def write_batches(output_root: str, date_folder: str, rows: list[Dict[str, str]]) -> Path:
    out_dir = Path(output_root) / date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{date_folder}"
    out_path = out_dir / f"{slug}.parquet"
    table = pa.Table.from_pylist(rows, schema=pa.schema([
        pa.field("prompt", pa.string()),
        pa.field("response", pa.string())
    ]))
    pq.write_table(table, out_path)
    return out_path

if __name__ == "__main__":
    # Example standalone run
    datef = os.getenv("DATE_FOLDER", "2024-01-01")
    rows = list(load_and_project(datef))
    if rows:
        p = write_batches("batches/mirror-merged", datef, rows)
        print(f"Wrote {len(rows)} rows to {p}")
    else:
        print("No rows extracted.")
```

#### 3) Lightning launcher with studio reuse
```python
# /opt/axentx/vanguard/backend/launch_train.py
import os
import subprocess
import time
from pathlib import Path

LIGHTNING_SCRIPT = Path(__file__).parent / "train.py"

def studio_name(date_folder: str, repo: str) -> str:
    slug = repo.replace("/", "-").replace("_", "-")
    return f"vanguard-train-{slug}-{date_folder}"

def is_studio_running(name: str) -> bool:
    # Lightweight check: lightning studio list --name <name> --status running
    try:
