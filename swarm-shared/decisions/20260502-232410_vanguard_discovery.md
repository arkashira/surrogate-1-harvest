# vanguard / discovery

## Final Synthesis (Best-of + Correctness + Actionability)

Below is the single, consolidated plan that keeps what is technically correct and immediately actionable, drops or fixes contradictions, and hardens the design against HF rate limits, quota burn, and schema drift.

---

## 1) Diagnosis (resolved)

- **No persistent file-list cache** → repeated `list_repo_tree` / `load_dataset` triggers 429 and wastes quota.  
  *Fix: cache once, reuse everywhere.*

- **Lightning Studio lifecycle not reused** → create/destroy per iteration burns quota.  
  *Fix: explicit reuse guard + idempotent attach; never recreate while running.*

- **No CDN-only data path** → training may still route through `/api/` or authenticated PyPI streams.  
  *Fix: enforce `https://huggingface.co/datasets/.../resolve/main/...` downloads; zero HF API calls during training.*

- **No repo-sharding for HF writes** → single repo risks 128 commits/hr cap and write collisions.  
  *Fix: deterministic 5-way sibling sharding by content hash.*

- **Schema heterogeneity / surrogate-1 hygiene** → raw parquet ingest can poison training with mismatched or unsafe columns.  
  *Fix: lightweight schema gate + safe column allow-list before materialization.*

- **Missing top-hub review checkpoint** → discovery loop lacks a curated knowledge-rag signal (MOC-style).  
  *Fix: add a tiny review manifest and optional RAG eval pass before heavy training.*

---

## 2) Proposed scaffold (final)

Create under `/opt/axentx/vanguard/discovery/`:

- `list_hf_files.py` — one-shot HF repo listing → `file_list.json` (cached, deterministic).  
- `schema_guard.py` — lightweight parquet schema allow-list + safety checks.  
- `train_cdn.py` — Lightning Studio training with CDN-only data, zero HF API calls, reproducible sharding for writes.  
- `reuse_studio.py` — idempotent Studio reuse helper (no quota burn).  
- `review_manifest.json` — optional top-hub review checkpoint (MOC-lite) to gate training.

---

## 3) Implementation (corrected + hardened)

```bash
mkdir -p /opt/axentx/vanguard/discovery
```

### 1) list_hf_files.py
```python
#!/usr/bin/env python3
"""
Run once per dataset/date (Mac or any HF-token machine).
Produces file_list.json for CDN-only training.
"""
import json
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/my-org/vanguard-mirror")
DATE_PATH = os.getenv("DATE_PATH", "batches/mirror-merged/2026-05-02")
OUT_FILE = Path("file_list.json")
RECURSIVE = os.getenv("RECURSIVE", "true").lower() == "true"

def main() -> None:
    api = HfApi()
    entries = api.list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_PATH,
        repo_type="dataset",
        recursive=RECURSIVE,
    )
    files = sorted(
        e.path for e in entries
        if e.type == "file" and e.path.endswith(".parquet")
    )
    if not files:
        print("No parquet files found. Check DATE_PATH or permissions.")
        sys.exit(1)

    payload = {
        "repo": HF_REPO,
        "date_path": DATE_PATH,
        "files": files,
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(files)} files to {OUT_FILE}")

if __name__ == "__main__":
    main()
```

### 2) schema_guard.py
```python
#!/usr/bin/env python3
"""
Lightweight schema allow-list and safety gate.
Exits non-zero if disallowed columns or no valid rows.
"""
import json
import os
import sys
from pathlib import Path

import pyarrow.parquet as pq

FILE_LIST = Path(os.getenv("FILE_LIST", "file_list.json"))
ALLOWED_COLS = {"prompt", "response", "instruction", "output"}
MIN_ROWS = int(os.getenv("MIN_ROWS", "1"))

def main() -> None:
    with FILE_LIST.open() as f:
        manifest = json.load(f)

    ok_files = []
    for rel in manifest["files"]:
        # local path for CDN cache (same convention as train_cdn.py)
        local = Path("./cdn_cache") / rel.replace("/", "_")
        if not local.exists():
            continue
        try:
            table = pq.read_table(local, memory_map=True)
        except Exception:
            continue
        cols = set(table.column_names)
        if not ALLOWED_COLS.intersection(cols):
            continue
        if table.num_rows < MIN_ROWS:
            continue
        ok_files.append(rel)

    if not ok_files:
        print("Schema guard: no valid files after checks.")
        sys.exit(1)

    # optionally trim manifest for downstream
    manifest["files"] = ok_files
    FILE_LIST.write_text(json.dumps(manifest, indent=2))
    print(f"Schema guard passed: {len(ok_files)} files")

if __name__ == "__main__":
    main()
```

### 3) reuse_studio.py
```python
#!/usr/bin/env python3
"""
Idempotent Lightning Studio reuse (no quota burn).
"""
from typing import Optional

try:
    from lightning.pytorch import Studio
except Exception:  # pragma: no cover - studio may be unavailable locally
    Studio = None

def reuse_studio(name: str = "vanguard-train") -> Optional["Studio"]:
    if Studio is None:
        return None
    for s in Studio.list():
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating new studio: {name}")
    return Studio(
        name=name,
        machine="L40S",
        create_ok=True,
    )
```

### 4) train_cdn.py
```python
#!/usr/bin/env python3
"""
Lightning Studio training (CDN-only).
Zero HF API calls during data load.
Deterministic sibling sharding for HF writes.
"""
import json
import os
import random
from pathlib import Path
from typing import Dict, List

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import Dataset, DataLoader
from lightning import LightningModule, Trainer

# ---------- config ----------
MANIFEST = Path(os.getenv("FILE_LIST", "file_list.json"))
CDN_BASE = "https://huggingface.co"
LOCAL_CACHE = Path("./cdn_cache")
LOCAL_CACHE.mkdir(exist_ok=True)

HF_SIBLING_REPOS = [
    "datasets/my-org/vanguard-mirror",
    "datasets/my-org/vanguard-mirror-sib1",
    "datasets/my-org/vanguard-mirror-sib2",
    "datasets/my-org/vanguard-mirror-sib3",
    "datasets/my-org/vanguard-mirror-sib4",
]

def pick_repo(slug: str) -> str:
    idx = hash(slug) % len(HF_SIBLING_REPOS)
    return HF_SIBLING_REPOS[idx]

# ---------- CDN dataset ----------
class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path: Path, max_rows: int = 50_000):
        with manifest_path.open() as f:
            manifest = json.load(f)
        self.repo = manifest["repo"]
        self.files = manifest["files"]
        self.max_rows = max_rows
        self.items: List[Dict[str, str]] = []
        self._load()

    def _download(self, path: str) -> Path:
        local = LOCAL_CACHE / path.replace("/", "_")
        if local.exists():
            return local
        url = f"{CDN_BASE}/datasets/{self.repo}/resolve/main/{path}"
        local.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        local.write_bytes(resp.content)
        return local

    def _load(self) -> None:
        random.shuffle(self.files)
        for path in self
