# vanguard / backend

## Final Integrated Solution

### Diagnosis (Consolidated)
- **No file-list manifest**: every training run performs authenticated `list_repo_tree` calls, burning HF API quota and risking 429s.
- **No CDN-only data loading**: training/ingest use `load_dataset(streaming=True)` or per-file API calls instead of public CDN URLs, causing auth-bound rate limits.
- **No commit-sharding**: all HF writes target a single repo, risking the 128 commits/hour cap.
- **No Lightning Studio reuse/idle-stop recovery**: each run creates new studios, wasting quota; idle-stop kills training and `.run()` fails instead of restarting.

### Architecture
1. **Mac-side manifest generation** (one authenticated API call per `(repo, dateFolder)`), persisted as `file-list.json`.
2. **Training uses CDN-only URLs** (zero auth/API calls during training).
3. **Deterministic 5-way repo sharding** for writes using hash-slug routing.
4. **Lightning Studio reuse + idle-stop recovery** in `lightning-lambda-prod` (L40S/H200).

---

### 1. `/opt/axentx/vanguard/backend/hf_strategy.py`

```python
# /opt/axentx/vanguard/backend/hf_strategy.py
import json
import hashlib
import os
import time
from pathlib import Path
from typing import List, Optional

import requests
from lightning import Studio, Teamspace, Machine

HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"

SIBLING_REPOS = [
    "axentx/vanguard-mirror-0",
    "axentx/vanguard-mirror-1",
    "axentx/vanguard-mirror-2",
    "axentx/vanguard-mirror-3",
    "axentx/vanguard-mirror-4",
]

CACHE_DIR = Path(os.getenv("VANGUARD_CACHE_DIR", "/opt/axentx/vanguard/.cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Manifest: repo + date_folder -> file list (cached)
# ---------------------------------------------------------------------------
def _cache_path(repo: str, date_folder: str) -> Path:
    safe = repo.replace("/", "_")
    return CACHE_DIR / f"{safe}_{date_folder}.json"

def list_date_files_cached(repo: str, date_folder: str, token: str) -> List[str]:
    """
    Return file paths for repo/date_folder using a persisted manifest.
    If missing, call HF API once (authenticated), then cache.
    """
    cache_file = _cache_path(repo, date_folder)
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    url = f"{HF_API_BASE}/repos/{repo}/tree/{date_folder}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 360))
        time.sleep(retry_after)
        resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    items = resp.json()
    paths = [f"{date_folder}/{it['path']}" for it in items if it.get("type") == "file"]
    cache_file.write_text(json.dumps(paths, indent=2))
    return paths

# ---------------------------------------------------------------------------
# CDN URLs (no auth)
# ---------------------------------------------------------------------------
def cdn_download_urls(repo: str, file_paths: List[str]) -> List[str]:
    base = f"{HF_CDN_BASE}/{repo}/resolve/main"
    return [f"{base}/{p}" for p in file_paths]

# ---------------------------------------------------------------------------
# Deterministic sharding for writes (mitigate 128/hr commit cap)
# ---------------------------------------------------------------------------
def pick_shard_repo(slug: str) -> str:
    digest = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return SIBLING_REPOS[digest % len(SIBLING_REPOS)]

# ---------------------------------------------------------------------------
# Lightning Studio reuse + idle-stop recovery
# ---------------------------------------------------------------------------
def ensure_lightning_studio(
    name: str,
    machine: str = "L40S",
    cloud: str = "lightning-lambda-prod",
) -> Studio:
    """
    Reuse a running studio or create/start one.
    If studio exists but is stopped (idle-stop), restart target machine.
    """
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name:
            if s.status == "running":
                return s
            s.start(machine=Machine(machine, cloud=cloud))
            return s

    return Studio(
        name=name,
        machine=Machine(machine, cloud=cloud),
        create_ok=True,
    )
```

---

### 2. `/opt/axentx/vanguard/backend/train.py`

```python
# /opt/axentx/vanguard/backend/train.py
import os
import json
import io
from pathlib import Path
from typing import Iterator, Tuple, Any

import pyarrow.parquet as pq
import requests

from .hf_strategy import list_date_files_cached, cdn_download_urls

HF_REPO = os.getenv("HF_REPO", "axentx/vanguard-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
HF_TOKEN = os.getenv("HF_TOKEN", "")  # only used once (Mac) to generate manifest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_manifest() -> list:
    manifest_path = Path(__file__).parent / ".cache" / f"{HF_REPO.replace('/', '_')}_{DATE_FOLDER}.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    # Fallback: generate once (should be done on Mac before training)
    return list_date_files_cached(HF_REPO, DATE_FOLDER, HF_TOKEN)

def _project_record(obj: dict) -> Tuple[str, str]:
    prompt = obj.get("prompt") or obj.get("input") or ""
    response = obj.get("response") or obj.get("output") or ""
    return prompt, response

# ---------------------------------------------------------------------------
# CDN streaming (zero auth/API calls)
# ---------------------------------------------------------------------------
def stream_cdn_files(file_paths: list, batch_size: int = 1024) -> Iterator[Tuple[str, str]]:
    urls = cdn_download_urls(HF_REPO, file_paths)
    for url in urls:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        content = resp.content

        if url.endswith(".jsonl"):
            for line in content.splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                prompt, response = _project_record(obj)
                if prompt and response:
                    yield prompt, response

        elif url.endswith(".parquet"):
            table = pq.read_table(io.BytesIO(content))
            for batch in table.to_batches(max_chunksize=batch_size):
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    prompt, response = _project_record(row.to_dict())
                    if prompt and response:
                        yield prompt, response

# ---------------------------------------------------------------------------
# Public API used by Lightning training job
# ---------------------------------------------------------------------------
def get_training_stream() -> Iterator[Tuple[str, str]]:
    file_paths = _load_manifest()
    yield from stream_cdn_files(file_paths)

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    count = 0
    for p, r in get_training_stream():
        count += 1
        if count <= 3:
            print(p[:80], "... ->", r[:80])
    print(f"Total pairs streamed: {count}")
```

---

### 3. `/opt/axentx/vanguard/backend/ingest.py`

```python
# /opt/axentx/vanguard/backend/ingest.py
import os
import io
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from pathlib import Path
from typing import List, Dict, Any

from .hf_strategy import pick_shard_repo

HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    raise RuntimeError("HF_TOKEN required for ingest")

HEADERS = {"Authorization": f
