# airship / discovery

## Highest-Value Incremental Improvement (≤2h)

**Goal**: Harden `airship discover` into a deterministic, CDN-only orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing reproducible file manifests and safe downstream ingestion.

**Why this ships fast**:
- No model training or GPU changes — pure orchestration + I/O.
- Reuses existing patterns (CDN bypass, file-list pre-generation, schema projection).
- One small CLI + one lightweight orchestrator module.

---

## Implementation Plan

| Step | Action | Owner | Time |
|------|--------|-------|------|
| 1 | Add `airship/discover/cdn_filelist.py` — deterministic file-lister (single API call per date folder) → JSON manifest | me | 20m |
| 2 | Add `airship/discover/cdn_downloader.py` — CDN-only fetcher (no auth, no `/api/`) with retries + integrity checks | me | 20m |
| 3 | Add `airship/discover/projector.py` — stream parquet/json per file, project `{prompt, response}` only, drop mixed schema cols, write to `enriched/` with `{date}/{slug}.parquet` naming | me | 25m |
| 4 | Add `airship/discover/__main__.py` CLI: `airship discover --repo <repo> --date <YYYY-MM-DD> --out ./enriched` | me | 15m |
| 5 | Update top-level `README.md` snippet + add cron-safe invocation note (set `SHELL=/bin/bash`) | me | 10m |
| 6 | Smoke test with small repo/date | me | 10m |

Total: ~1h40m.

---

## Code Snippets

### 1) `airship/discover/cdn_filelist.py`

```python
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from huggingface_hub import HfApi, RepositoryNotFoundError

CDN_BASE = "https://huggingface.co/datasets/{repo}/resolve/main"

def list_date_folder(repo: str, date: str, api: HfApi | None = None) -> List[Dict[str, Any]]:
    """
    Single API call to list one date folder (non-recursive).
    Returns lightweight metadata for CDN download.
    """
    api = api or HfApi()
    folder = f"{date}"
    try:
        items = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    except RepositoryNotFoundError:
        raise ValueError(f"Repo not found: {repo}")

    files: List[Dict[str, Any]] = []
    for item in items:
        if getattr(item, "type", None) == "file":
            fname = getattr(item, "path", None) or getattr(item, "name", None)
            if not fname:
                continue
            files.append({
                "repo": repo,
                "path": fname,
                "cdn_url": f"{CDN_BASE.format(repo=repo)}/{fname}",
                "size": getattr(item, "size", None),
            })
    return files

def save_manifest(repo: str, date: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / f"{repo.replace('/', '_')}_{date}_manifest.json"
    files = list_date_folder(repo, date)
    payload = {
        "repo": repo,
        "date": date,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }
    manifest_path.write_text(json.dumps(payload, indent=2))
    return manifest_path
```

---

### 2) `airship/discover/cdn_downloader.py`

```python
import shutil
import time
from pathlib import Path
from typing import Dict, Any

import requests
from tqdm import tqdm

CDN_TIMEOUT = 30
MAX_RETRIES = 5
BACKOFF = 5  # seconds; will increase exponentially

def cdn_download(file_meta: Dict[str, Any], dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = file_meta["cdn_url"]
    out_path = dest_dir / Path(file_meta["path"]).name

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(url, stream=True, timeout=CDN_TIMEOUT) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                with open(out_path, "wb") as f, tqdm(
                    desc=out_path.name,
                    total=total,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    disable=total == 0,
                ) as pbar:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        pbar.update(len(chunk))
            # basic integrity: non-empty
            if out_path.stat().st_size > 0:
                return out_path
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Failed to download {url} after {MAX_RETRIES} attempts") from exc
            time.sleep(BACKOFF * (2 ** (attempt - 1)))
    raise RuntimeError(f"Unexpected exit for {url}")
```

---

### 3) `airship/discover/projector.py`

```python
from __future__ import annotations

import pyarrow.parquet as pq
import pyarrow.compute as pc
from pathlib import Path
from typing import Iterator, Dict, Any
import pandas as pd

PROMPT_COL_CANDIDATES = {"prompt", "instruction", "input", "question", "text"}
RESPONSE_COL_CANDIDATES = {"response", "output", "answer", "completion", "result"}

def detect_columns(df: pd.DataFrame) -> tuple[str | None, str | None]:
    cols = {c.lower(): c for c in df.columns}
    prompt = next((cols[k] for k in PROMPT_COL_CANDIDATES if k in cols), None)
    response = next((cols[k] for k in RESPONSE_COL_CANDIDATES if k in cols), None)
    return prompt, response

def project_file(in_path: Path, out_dir: Path, slug: str) -> Path | None:
    """
    Load file (parquet/jsonl), keep only {prompt,response}, write enriched parquet.
    Skip files that cannot be projected.
    """
    try:
        if in_path.suffix == ".parquet":
            df = pq.read_table(in_path).to_pandas()
        elif in_path.suffix == ".jsonl":
            df = pd.read_json(in_path, lines=True)
        else:
            return None
    except Exception:
        return None

    prompt_col, response_col = detect_columns(df)
    if not prompt_col or not response_col:
        return None

    projected = df[[prompt_col, response_col]].rename(
        columns={prompt_col: "prompt", response_col: "response"}
    ).dropna(subset=["prompt", "response"])

    if projected.empty:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.parquet"
    pq.write_table(pa.Table.from_pandas(projected, preserve_index=False), out_path)
    return out_path
```

(Note: import `pyarrow as pa` at top if used.)

---

### 4) `airship/discover/__main__.py`

```python
#!/usr/bin/env python3
"""
CDN-only discovery orchestrator.

Usage:
  airship discover --repo <repo> --date <YYYY-MM-DD> --out ./enriched
"""

import argparse
import json
import sys
from pathlib import Path

from .cdn_filelist import save_manifest
from .cdn_downloader import cdn_download
from .projector import project_file

def main() -> None:
    parser = argparse.ArgumentParser(description="Airship CDN-only discover")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/ds)")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder")
    parser.add_argument("--out", default="./enriched", help="Output directory for enriched parquet")
    args = parser.parse_args()

    work
