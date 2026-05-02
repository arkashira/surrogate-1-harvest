# vanguard / backend

## 1. Diagnosis
- No persistent file-list cache for HF repos → training scripts repeatedly call `list_repo_tree`/`load_dataset` and hit 429 rate limits.
- Lightning Studio lifecycle not reused → quota burned on recreation; idle-stop deaths silently kill training.
- No CDN-bypass data loader → training still uses HF API (`load_dataset`) instead of raw CDN fetches, wasting rate-limit budget.
- Schema drift risk: ingestion writes mixed-schema parquet into `enriched/` instead of projecting to `{prompt,response}` and storing attribution in filename.
- No studio health-check before `.run()` → Lightning idle-stop kills jobs without restart.

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/training/file_cache.py` + update `/opt/axentx/vanguard/backend/training/train.py` (or create if absent) to:
- Single API call to `list_repo_tree` per date folder → write `file_list.json` to `cache/`.
- Train loader to use CDN-only URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero auth/API calls.
- Add studio reuse + idle-stop guard before each training run.

## 3. Implementation

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/backend/training/cache
touch /opt/axentx/vanguard/backend/training/__init__.py
```

`/opt/axentx/vanguard/backend/training/file_cache.py`
```python
import json
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict

import requests

try:
    from huggingface_hub import HfApi, list_repo_tree
except ImportError:
    HfApi = None
    list_repo_tree = None

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

HF_CDN_BASE = "https://huggingface.co/datasets"

def _cache_path(repo: str, folder: str) -> Path:
    slug = repo.replace("/", "_")
    folder_slug = folder.strip("/").replace("/", "_") or "root"
    return CACHE_DIR / f"{slug}__{folder_slug}.json"

def list_and_cache_files(
    repo: str,
    folder: str = "",
    cache_ttl_hours: int = 24,
    use_api: bool = True
) -> List[Dict[str, str]]:
    """
    List files in a repo folder once and cache to disk.
    Returns list of dicts: {"path": "...", "cdn_url": "...", "size": ...}
    """
    cache_file = _cache_path(repo, folder)

    # Use cache if fresh
    if cache_file.exists() and cache_ttl_hours > 0:
        age_h = (datetime.utcnow().timestamp() - cache_file.stat().st_mtime) / 3600
        if age_h < cache_ttl_hours:
            try:
                return json.loads(cache_file.read_text())
            except Exception:
                pass  # fall through to refresh

    if not use_api or HfApi is None or list_repo_tree is None:
        raise RuntimeError("HF API unavailable and no cache present")

    # Single API call (non-recursive) per folder
    items = list(list_repo_tree(repo=repo, path=folder, recursive=False))
    files = []
    for item in items:
        if item.type != "file":
            continue
        rel_path = item.path if not folder else os.path.join(folder, item.path).lstrip("/")
        cdn_url = f"{HF_CDN_BASE}/{repo}/resolve/main/{rel_path}"
        files.append({
            "path": rel_path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
            "lfs": getattr(item, "lfs", None) is not None
        })

    cache_file.write_text(json.dumps(files, indent=2))
    return files

def stream_cdn_parquet(cdn_url: str, chunk_size: int = 1024 * 1024):
    """Stream a public parquet via CDN (no auth)."""
    with requests.get(cdn_url, stream=True, timeout=30) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=chunk_size):
            yield chunk
```

`/opt/axentx/vanguard/backend/training/train.py` (minimal update/create)
```python
import os
import pyarrow.parquet as pq
import pyarrow.compute as pc
import io
from typing import Iterator, Dict

from file_cache import list_and_cache_files, stream_cdn_parquet

REPO = os.getenv("HF_DATASET_REPO", "your-org/your-dataset")
FOLDER = os.getenv("HF_DATASET_FOLDER", "batches/mirror-merged/2026-05-02")

def cdn_file_generator() -> Iterator[Dict[str, str]]:
    """Yield {prompt, response} rows from cached CDN parquet files."""
    files = list_and_cache_files(REPO, FOLDER, cache_ttl_hours=24, use_api=True)
    for f in files:
        if not f["path"].endswith(".parquet"):
            continue
        try:
            buf = io.BytesIO()
            for chunk in stream_cdn_parquet(f["cdn_url"]):
                buf.write(chunk)
            buf.seek(0)
            table = pq.read_table(buf)
            # Project to minimal schema; ignore extra cols to avoid schema drift
            if "prompt" in table.column_names and "response" in table.column_names:
                table = table.select(["prompt", "response"])
            else:
                # Best-effort fallback: try common aliases
                prompt_col = next((c for c in table.column_names if "prompt" in c.lower()), None)
                response_col = next((c for c in table.column_names if "response" in c.lower() or "completion" in c.lower()), None)
                if prompt_col and response_col:
                    table = table.select([prompt_col, response_col]).rename_columns(["prompt", "response"])
                else:
                    continue

            for batch in table.to_batches(max_chunksize=1024):
                cols = batch.columns
                for i in range(batch.num_rows):
                    yield {"prompt": str(cols[0][i].as_py()), "response": str(cols[1][i].as_py())}
        except Exception as exc:
            print(f"Skipping {f['path']} due to error: {exc}")
            continue

# Example usage in training loop (replace with your dataloader):
if __name__ == "__main__":
    count = 0
    for row in cdn_file_generator():
        # Replace with tokenization + training step
        print(f"Row {count}: prompt={row['prompt'][:60]}...")
        count += 1
        if count >= 10:
            break
```

Lightning Studio reuse + idle-stop guard (example launcher snippet)
```python
# launcher.py (run from Mac orchestration)
from lightning import Studio, Machine, Teamspace
import time

STUDIO_NAME = "vanguard-l40s-training"

def ensure_running_studio():
    team = Teamspace()
    for s in team.studios:
        if s.name == STUDIO_NAME:
            if s.status == "Running":
                print(f"Reusing running studio: {STUDIO_NAME}")
                return s
            else:
                print(f"Restarting stopped studio: {STUDIO_NAME}")
                # Start with available free-tier machine (L40S if in public; fallback to L40S/L4)
                s.start(machine=Machine.L40S)
                return s
    # Create if missing
    studio = Studio.create(
        name=STUDIO_NAME,
        machine=Machine.L40S,
        create_ok=True
    )
    return studio

def run_training():
    studio = ensure_running_studio()
    # Guard: if studio not running after start, wait/retry
    for _ in range(6):
        studio.refresh()
        if studio.status == "Running":
            break
        time.sleep(10)
    else:
        raise RuntimeError("Studio failed to start")

    # Execute training inside studio (example)
    job = studio.run(
        command=["python", "backend/training/train.py"],
        cwd="/workspace/vanguard"
    )
    return job

if __name__ == "__
