# vanguard / quality

## Final Consolidated Implementation

**Core diagnosis (merged, de-duplicated):**
- No persisted `(repo, dateFolder) → file-list` manifest → repeated authenticated `list_repo_tree`/`load_dataset` calls burn HF API quota and risk 429s.
- Training uses heterogeneous repos/files → `pyarrow.CastError` from mixed schemas and non-canonical column names.
- Data ingestion keeps extra metadata columns (`source`, `ts`) instead of clean `{prompt,response}` → violates Surrogate-1 schema rules.
- No CDN bypass → authenticated API calls during training instead of zero-auth CDN fetches.
- No Lightning Studio reuse → new Studio per run wastes quota.

**Single prioritized change:**
Add `/opt/axentx/vanguard/training/file_manifest.py` and update `/opt/axentx/vanguard/training/train.py` to:
- Build/persist manifest with one authenticated call per `(repo, dateFolder)`.
- Use CDN-only URLs for training data fetches (no auth, no API quota).
- Project to canonical `{prompt, response}` at parse time; drop all other columns.
- Reuse a Running Lightning Studio before creating a new one.

---

### 1. `/opt/axentx/vanguard/training/file_manifest.py`

```python
#!/usr/bin/env python3
"""
Build and use a (repo, date_folder) -> file-list manifest to avoid HF API
rate limits during training. Training uses CDN-only fetches.
"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

try:
    from huggingface_hub import list_repo_tree, hf_hub_download, HfApi
except ImportError:
    list_repo_tree = None
    hf_hub_download = None
    HfApi = None


MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)


def _safe_repo(repo: str) -> str:
    return repo.replace("/", "__")


def manifest_path(repo: str, date_folder: str) -> Path:
    return MANIFEST_DIR / f"{_safe_repo(repo)}__{date_folder}.json"


def build_manifest(
    repo: str,
    date_folder: str,
    recursive: bool = False,
    repo_type: str = "dataset",
    force: bool = False,
) -> List[str]:
    """
    Single authenticated call to list_repo_tree for one date folder.
    Returns list of file paths (relative to repo root) under date_folder.
    Persists to manifests/ for reuse.
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not installed")

    out_path = manifest_path(repo, date_folder)
    if out_path.exists() and not force:
        return json.loads(out_path.read_text())

    # Avoid recursive explosion; list only immediate folder contents.
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=recursive, repo_type=repo_type)
    files = sorted(item.rfilename for item in tree if item.type == "file")

    out_path.write_text(json.dumps(files, indent=2))
    return files


def cdn_url(repo: str, file_path: str) -> str:
    """
    Public CDN URL that bypasses HF API auth and rate limits.
    Works for datasets/ models/ spaces/ when file_path is repo-relative.
    """
    # Normalize: ensure file_path is repo-relative (not including repo prefix)
    if file_path.startswith("datasets/"):
        file_path = "/".join(file_path.split("/")[2:])  # datasets/<repo>/<path> -> <path>
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"


def canonical_columns(df):
    """
    Project DataFrame to canonical {prompt, response}.
    Drops all other columns including metadata (source, ts, etc.).
    """
    import pandas as pd

    if not isinstance(df, pd.DataFrame):
        raise ValueError("Expected pandas DataFrame")

    # Canonical names
    prompt_candidates = {"prompt", "question", "input", "instruction"}
    response_candidates = {"response", "answer", "output", "completion"}

    # Case-insensitive match
    lower_to_orig = {c.lower(): c for c in df.columns}
    prompt_col = next((lower_to_orig[c] for c in lower_to_orig if c in prompt_candidates), None)
    response_col = next((lower_to_orig[c] for c in lower_to_orig if c in response_candidates), None)

    # Fallback: first two non-metadata columns
    if prompt_col is None or response_col is None:
        non_meta = [c for c in df.columns if c.lower() not in {"source", "ts", "index", "id", "split"}]
        if len(non_meta) >= 2:
            prompt_col, response_col = non_meta[0], non_meta[1]
        elif len(non_meta) == 1:
            prompt_col, response_col = non_meta[0], non_meta[0]
        else:
            raise ValueError("Could not determine prompt/response columns")

    out = pd.DataFrame({
        "prompt": df[prompt_col].astype(str),
        "response": df[response_col].astype(str),
    })
    return out.dropna(subset=["prompt", "response"]).reset_index(drop=True)


def load_parquet_project_to_dialogue(
    repo: str,
    file_path: str,
    use_cdn: bool = True,
    timeout: int = 30,
):
    """
    Download one file and project to {prompt, response}.
    Prefer CDN fetch; fallback to hf_hub_download.
    """
    import pandas as pd
    from io import BytesIO
    import requests

    if use_cdn:
        url = cdn_url(repo, file_path)
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            df = pd.read_parquet(BytesIO(r.content))
            return canonical_columns(df)
        except Exception:
            # fallback to authenticated download
            pass

    if hf_hub_download is None:
        raise RuntimeError("huggingface_hub not installed")

    # Interpret repo/file_path for hf_hub_download
    # Support formats:
    # - datasets/<repo>/<path>
    # - <repo>/<path>
    parts = file_path.split("/")
    if len(parts) >= 2 and parts[0] == "datasets":
        repo_id = parts[1]
        subpath = "/".join(parts[2:])
    else:
        repo_id = repo
        subpath = "/".join(parts)

    local_path = hf_hub_download(
        repo_id=repo_id,
        filename=subpath,
        repo_type="dataset",
    )
    df = pd.read_parquet(local_path)
    return canonical_columns(df)


def build_or_load_manifest(
    repo: str,
    date_folder: str,
    recursive: bool = False,
    repo_type: str = "dataset",
    force: bool = False,
) -> List[str]:
    """Convenience: build or load manifest and return file list."""
    return build_manifest(
        repo=repo,
        date_folder=date_folder,
        recursive=recursive,
        repo_type=repo_type,
        force=force,
    )
```

---

### 2. `/opt/axentx/vanguard/training/train.py`

```python
#!/usr/bin/env python3
"""
Surrogate-1 training entrypoint:
- Build/use manifest to avoid HF API calls during training.
- Fetch data via CDN (no auth).
- Reuse Running Lightning Studio.
"""
import json
import os
import sys
from pathlib import Path
from typing import List

try:
    import lightning as L
    from lightning.pytorch import Trainer
    from lightning.pytorch.strategies import DDPStrategy
except ImportError:
    print("Install lightning before running: pip install lightning")
    sys.exit(1)

from file_manifest import (
    build_or_load_manifest,
    cdn_url,
    load_parquet_project_to_dialogue,
)


REPO = os.getenv("HF_DATASET_REPO", "your-org/your-dataset")
DATE_FOLDER = os.getenv("DATASET_DATE_FOLDER", "2026-04-29")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8
