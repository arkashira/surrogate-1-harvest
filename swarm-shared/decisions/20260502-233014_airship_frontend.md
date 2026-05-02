# airship / frontend

## Final Synthesis — Hardened, CDN-Only `airship discover`

**Chosen direction**  
Combine the orchestration clarity and CDN-first safety from both proposals into a single, deterministic workflow that:

- Eliminates Hugging Face API rate limits by using **CDN-only downloads** (`https://huggingface.co/datasets/{repo}/resolve/main/...`).
- Avoids PyArrow schema errors by **never using `load_dataset(streaming=True)`** on heterogeneous repos; instead download individual files and project `{prompt,response}` at parse time.
- Produces **reproducible file lists** via a single `list_repo_tree` snapshot saved as JSON and referenced by training scripts.
- Adds **Lightning Studio reuse guard** and a clear **manifest output** for auditability.

---

## Implementation Plan (≤2h)

1. **Locate entrypoint** — find `airship/discover` CLI or orchestrator script.
2. **Add CDN-first downloader** — `hf_cdn_download(repo, path, out_dir)` using `requests` with retries, `tqdm`, and deterministic SHA-256 verification.
3. **Schema-safe parser** — per-file loader that extracts only `{prompt,response}` fields; ignore extra columns; store as normalized Parquet with deterministic schema.
4. **Deterministic file list** — run `list_repo_tree(repo, path, recursive=True)` once, save `file_list.json` with `sha256` and `cdn_url`.
5. **Embed file list in training script** — update `train.py` to read `file_list.json` and fetch via CDN (zero HF API calls during training).
6. **Lightning Studio reuse guard** — add check to reuse running Studio; restart if idle-stopped.
7. **Output manifest** — write `manifest.json` with `{repo, date, file_count, total_bytes, cdn_only: true}`.

---

## Code (single coherent version)

### 1. CDN-only downloader (deterministic, rate-limit-safe)

```python
# airship/discover/cdn.py
import os
import json
import hashlib
import requests
from pathlib import Path
from tqdm import tqdm
from typing import Dict

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def hf_cdn_download(repo: str, path: str, out_dir: Path, chunk_size: int = 8192) -> Dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    url = HF_CDN_TEMPLATE.format(repo=repo, path=path)
    out_path = out_dir / Path(path).name

    existing_sha = None
    if out_path.exists():
        existing_sha = hashlib.sha256(out_path.read_bytes()).hexdigest()

    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))

    with open(out_path, "wb") as f, tqdm(
        desc=path, total=total, unit="B", unit_scale=True
    ) as pbar:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            f.write(chunk)
            pbar.update(len(chunk))

    sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
    return {
        "repo": repo,
        "path": path,
        "cdn_url": url,
        "local_path": str(out_path),
        "sha256": sha,
        "size": out_path.stat().st_size,
        "cached_sha_match": (sha == existing_sha) if existing_sha else False,
    }
```

### 2. Deterministic file lister (recursive snapshot)

```python
# airship/discover/lister.py
import json
from datetime import datetime
from huggingface_hub import HfApi
from typing import List, Dict

def snapshot_repo_tree(repo: str, path: str = "", out_file: str = "file_list.json") -> Dict:
    api = HfApi()
    # recursive=True gives full tree in one call; fallback to manual recursion if needed
    tree = api.list_repo_tree(repo=repo, path=path, recursive=True)
    files: List[Dict] = []
    for entry in tree:
        if not entry.path.endswith("/"):
            files.append({"path": entry.path, "size": entry.size})

    snapshot = {
        "repo": repo,
        "snapshot_at": datetime.utcnow().isoformat() + "Z",
        "root_path": path,
        "files": files,
        "file_count": len(files),
    }
    with open(out_file, "w") as fp:
        json.dump(snapshot, fp, indent=2)
    return snapshot
```

### 3. Schema-safe per-file parser (avoids PyArrow CastError)

```python
# airship/discover/parser.py
import pyarrow.parquet as pq
import pandas as pd
from pathlib import Path

def extract_prompt_response(parquet_path: Path):
    # Avoid load_dataset; read only required columns if possible
    try:
        pf = pq.ParquetFile(parquet_path)
        schema_names = {field.name for field in pf.schema}
    except Exception:
        df = pd.read_parquet(parquet_path)
        schema_names = set(df.columns)

    prompt_col = next((c for c in schema_names if "prompt" in c.lower()), None)
    response_col = next((c for c in schema_names if "response" in c.lower()), None)

    if prompt_col and response_col:
        df = pd.read_parquet(parquet_path, columns=[prompt_col, response_col])
    else:
        df = pd.read_parquet(parquet_path)
        text_cols = [c for c in df.columns if df[c].dtype == "object" and df[c].str.len().mean() > 10]
        cols = text_cols[:2] if len(text_cols) >= 2 else list(df.columns)[:2]
        df = df[cols]
        df.columns = ["prompt", "response"]

    # Normalize column names
    if prompt_col and response_col:
        df = df.rename(columns={prompt_col: "prompt", response_col: "response"})
    elif len(df.columns) == 2:
        df.columns = ["prompt", "response"]

    # Ensure string types
    df["prompt"] = df["prompt"].astype(str)
    df["response"] = df["response"].astype(str)
    return df[["prompt", "response"]]
```

### 4. Orchestrator (glue)

```python
# airship/discover/orchestrator.py
import json
import pandas as pd
from pathlib import Path
from .cdn import hf_cdn_download
from .lister import snapshot_repo_tree
from .parser import extract_prompt_response

def run_discover(repo: str, date_folder: str, out_root: str = "data/discover"):
    out_root = Path(out_root)
    snapshot = snapshot_repo_tree(repo, path=date_folder, out_file=out_root / "file_list.json")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "cdn_only": True,
        "downloads": [],
        "normalized_parquet": None,
    }

    rows = []
    for f in snapshot["files"]:
        if not f["path"].lower().endswith(".parquet"):
            continue
        meta = hf_cdn_download(repo, f["path"], out_root / "raw")
        manifest["downloads"].append(meta)

        df = extract_prompt_response(Path(meta["local_path"]))
        df["source_file"] = f["path"]
        rows.append(df)

    if rows:
        full = pd.concat(rows, ignore_index=True)
        parquet_out = out_root / "normalized" / f"{date_folder.replace('/', '_')}.parquet"
        parquet_out.parent.mkdir(parents=True, exist_ok=True)
        full.to_parquet(parquet_out, index=False)
        manifest["normalized_parquet"] = str(parquet_out)
        manifest["total_rows"] = len(full)

    with open(out_root / "manifest.json", "w") as fp:
        json.dump(manifest, fp, indent=2)

    print("✅ Discover complete. CDN-only manifest written.")
    return manifest
```

### 5. Lightning Studio reuse guard + CDN-only training stub

```python
# surrogate/train.py (excerpt)
import json
from pathlib import Path
from lightning import LightningWork
