# airship / discovery

## Final Integrated Solution  
*(Best parts merged, contradictions resolved for correctness + concrete actionability)*

### Core Improvements (Highest ROI)
1. **CDN-first ingestion** — eliminate HF API calls during training by pre-listing once and downloading via public CDN.  
2. **Deterministic sibling-repo sharding** — bypass 128/hr commit cap with hash-based repo selection.  
3. **Studio lifecycle guard** — reuse running studios; auto-restart if stopped to prevent idle-timeout training loss.  
4. **Schema projection at parse time** — download only needed files and extract `{prompt, response}` to minimize I/O and memory.

**Estimated effort**: 1.5–2 hours (including tests and integration).

---

## Implementation Plan (≤2h)

### 1. Pre-list file paths (Mac/orchestrator)
- Single `list_repo_tree` call per date folder after rate-limit window.  
- Save JSON with repo, date_folder, and sorted file list.  
- Embed JSON in training script; zero HF API calls during data loading.

### 2. CDN-only loader with caching
- Download via `https://huggingface.co/datasets/{repo}/resolve/main/{file_path}` (no auth).  
- Local cache directory to avoid re-downloads across epochs/restarts.  
- Project parquet to `{prompt, response}` at parse time; ignore extra columns.  
- Skip corrupt files with logging; continue iteration.

### 3. Deterministic sibling-repo sharding for writes
- Formula: `repo = BASE_REPO if idx == 0 else f"{BASE_REPO}-{idx}"`  
  where `idx = hash(slug) % N_SIBLINGS` (0-based).  
- Use SHA-256 for stable distribution; configurable `N_SIBLINGS` (default 5 → 6 repos total = 640 writes/hr aggregate).  
- Central upload helper that picks repo and uploads file.

### 4. Studio lifecycle guard
- Before `.run()`, check status; if stopped, restart with target machine.  
- Reuse running studios to save quota and avoid cold starts.  
- Retry with backoff on transient failures; cap retries (default 3).  
- Ensure machine type is explicit (L40S) and startup wait is sufficient.

### 5. Training integration
- Entrypoint accepts `--file-list` (JSON from step 1) and `--cache-dir`.  
- Stream dataset via CDN loader; do not materialize full dataset in memory.  
- Optionally wrap training run with Studio guard for end-to-end reliability.

---

## Code Snippets (Integrated + Corrected)

### `scripts/cdn_file_list.py`
```python
#!/usr/bin/env python3
"""
Generate CDN file list for a date folder.
Run from Mac/orchestrator after rate-limit window clears.
"""
import json
import sys
from pathlib import Path

from huggingface_hub import HfApi

API = HfApi()
REPO = "axentx/surrogate-dataset-mirror"
OUT_DIR = Path("file_lists")
OUT_DIR.mkdir(exist_ok=True)

def list_date_folder(date_folder: str) -> list[str]:
    """List files in date folder (non-recursive)."""
    tree = API.list_repo_tree(
        repo_id=REPO,
        path=date_folder,
        recursive=False,
    )
    # Keep only file paths (not dirs)
    files = [item.path for item in tree if item.type == "file"]
    return sorted(files)

def save_file_list(date_folder: str, files: list[str]) -> Path:
    out_path = OUT_DIR / f"{date_folder.replace('/', '_')}.json"
    with open(out_path, "w") as f:
        json.dump({"repo": REPO, "date_folder": date_folder, "files": files}, f, indent=2)
    return out_path

if __name__ == "__main__":
    date_folder = sys.argv[1] if len(sys.argv) > 1 else "2026-04-29"
    files = list_date_folder(date_folder)
    out = save_file_list(date_folder, files)
    print(f"Saved {len(files)} files to {out}")
```

### `surrogate/ingest/cdn_loader.py`
```python
#!/usr/bin/env python3
"""
CDN-only loader for training. Uses pre-generated file list.
Zero HF API calls during data loading.
"""
import json
from pathlib import Path
from typing import Iterator, Tuple

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

def cdn_download(repo: str, file_path: str, cache_dir: Path) -> Path:
    """Download via CDN (no auth)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Flatten path safely; keep extension
    safe_name = file_path.replace("/", "_")
    cache_file = cache_dir / safe_name
    if cache_file.exists():
        return cache_file

    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    cache_file.write_bytes(resp.content)
    return cache_file

def project_to_prompt_response(parquet_path: Path) -> Iterator[Tuple[str, str]]:
    """Project parquet to (prompt, response) pairs."""
    table = pq.read_table(parquet_path)
    # Expect columns: prompt, response (others ignored)
    if "prompt" not in table.column_names or "response" not in table.column_names:
        raise ValueError(f"Missing required columns in {parquet_path}")

    prompts = table.column("prompt").to_pylist()
    responses = table.column("response").to_pylist()
    for p, r in zip(prompts, responses):
        if p and r:
            yield str(p).strip(), str(r).strip()

def load_dataset_from_cdn(file_list_path: Path, cache_dir: Path) -> Iterator[Tuple[str, str]]:
    """Load dataset using CDN downloads only."""
    with open(file_list_path) as f:
        meta = json.load(f)
    repo = meta["repo"]
    files = meta["files"]

    for file_path in tqdm(files, desc="Loading via CDN"):
        try:
            local_path = cdn_download(repo, file_path, cache_dir)
            yield from project_to_prompt_response(local_path)
        except Exception as e:
            print(f"Skipping {file_path}: {e}")
            continue
```

### `surrogate/ingest/sharded_upload.py`
```python
#!/usr/bin/env python3
"""
Deterministic sibling-repo sharding to bypass 128/hr commit cap.
"""
import hashlib
from pathlib import Path

from huggingface_hub import HfApi

API = HfApi()
BASE_REPO = "axentx/surrogate-dataset-mirror"
N_SIBLINGS = 5  # 5 siblings + base = 6 repos total

def pick_repo(slug: str, n_siblings: int = N_SIBLINGS) -> str:
    """Deterministically pick repo based on slug hash."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n_siblings
    if idx == 0:
        return BASE_REPO
    return f"{BASE_REPO}-{idx}"

def upload_sharded(slug: str, file_path: Path, subpath: str, n_siblings: int = N_SIBLINGS) -> str:
    """Upload to deterministically chosen repo."""
    repo = pick_repo(slug, n_siblings=n_siblings)
    API.upload_file(
        path_or_fileobj=str(file_path),
        path_in_repo=subpath,
        repo_id=repo,
    )
    return repo
```

### `surrogate/training/studio_guard.py`
```python
#!/usr/bin/env python3
"""
Studio lifecycle guard: reuse running studios, restart if stopped.
"""
import time

from lightning import Studio, Machine

def get_or_create_studio(name: str, machine: Machine = Machine.L40S) -> Studio:
    """Get running studio or create new one."""
    from lightning import Teamspace

    for s in Teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}")
            return s

    print(f"Creating studio: {name}")
    return Studio(
        name=name,

