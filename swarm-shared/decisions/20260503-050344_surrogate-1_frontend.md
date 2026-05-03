# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`.

### Changes

1. Add `bin/worker.py` — single, deterministic shard worker that:
   - Reads a pre-computed `manifest.json` (date folder → file list) produced once per run by the orchestrator (or cached from earlier `list_repo_tree` call).
   - Downloads only its 1/16 slice via HF CDN (`resolve/main/...`) with **no Authorization header** → bypasses `/api/` rate limits entirely.
   - Projects each file to `{prompt, response}` at parse time (never loads full heterogeneous schema into one pyarrow table).
   - Deduplicates via centralized SQLite md5 store (existing `lib/dedup.py`).
   - Emits `shard-<N>-<HHMMSS>.jsonl` into `batches/public-merged/<date>/`.
   - Exits with non-zero on unrecoverable errors (GitHub Actions will retry the shard).

2. Update `bin/dataset-enrich.sh` → thin wrapper that:
   - Invokes `python3 bin/worker.py` with `SHARD_ID`, `MANIFEST_PATH`, `DATE`.
   - Keeps executable bit; Bash shebang preserved for cron/CI compatibility.

3. Add `bin/build-manifest.py` (run once per cron tick, before matrix starts):
   - Uses HF API **once** (after rate-limit window) to `list_repo_tree(path, recursive=False)` for the target date folder.
   - Writes `manifest.json` to repo root (or uploads as workflow artifact) so all 16 shards share the same file list without per-shard API calls.

4. Update `.github/workflows/ingest.yml`:
   - Add a one-off job step (or separate job) to generate `manifest.json` and pass it to the matrix via `artifacts` or `outputs`.
   - Keep 16-shard matrix; each shard runs `bin/dataset-enrich.sh` (now Python-backed).
   - Set `SHELL=/bin/bash` in workflow defaults to avoid wrapper exec issues.

5. Minor hygiene:
   - Ensure `lib/dedup.py` uses WAL mode and connection pooling to avoid SQLite contention across shards (each shard is isolated on its own runner, but central store lives on HF Space — accessed via REST or shared volume; if contention appears, switch to advisory-file locking or idempotent upserts).
   - Add `.python-version` or pin `pyarrow>=14` in `requirements.txt`.

---

### Code snippets

#### `bin/build-manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest.json for a date folder (e.g. 2026-05-03).
Run once per cron tick before the 16-shard matrix starts.
"""
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUT = os.getenv("OUT", "manifest.json")

def main() -> None:
    api = HfApi()
    # Single API call; recursive=False avoids pagination explosion
    tree = api.list_repo_tree(repo_id=HF_REPO, path=DATE, recursive=False)
    files = [f.rfilename for f in tree if f.rfilename.endswith((".jsonl", ".parquet", ".json"))]

    manifest = {
        "date": DATE,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
    }

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"Wrote {len(files)} files to {OUT}")

if __name__ == "__main__":
    main()
```

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker.

Usage:
  SHARD_ID=3 python3 bin/worker.py --manifest manifest.json --date 2026-05-03
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate, mark_seen  # type: ignore

def shard_files(all_files: list[str], shard_id: int, total_shards: int = 16) -> list[str]:
    return [f for i, f in enumerate(all_files) if i % total_shards == shard_id]

def hash_slug(filepath: str) -> str:
    return hashlib.md5(filepath.encode()).hexdigest()

def project_to_pair(obj: Dict[str, Any], filepath: str) -> Dict[str, str]:
    """
    Best-effort projection to {prompt,response}.
    Add more schema adapters here as needed.
    """
    # Common patterns observed in surrogate-1 data
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""

    # Fallback: if flat text column exists and looks like conv, split crudely
    if not prompt and not response:
        for v in obj.values():
            if isinstance(v, str) and len(v) > 20:
                parts = v.split("\n\n", 1)
                if len(parts) == 2:
                    prompt, response = parts[0], parts[1]
                    break
                # else skip — will be filtered out later

    # Ensure strings
    prompt = str(prompt).strip()
    response = str(response).strip()

    slug = hash_slug(filepath)
    return {"prompt": prompt, "response": response, "slug": slug}

def stream_jsonl(url: str) -> Iterable[Dict[str, Any]]:
    with requests.get(url, timeout=60) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if line:
                yield json.loads(line)

def stream_parquet(url: str) -> Iterable[Dict[str, Any]]:
    # Parquet via CDN: stream to temp file to avoid loading all into RAM
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192):
                tmp.write(chunk)
        tmp_path = tmp.name
    try:
        table = pq.read_table(tmp_path, columns=[])
        # Read all columns; project later to avoid schema conflicts
        df = table.to_pandas()
        for _, row in df.iterrows():
            yield row.to_dict()
    finally:
        os.unlink(tmp_path)

def worker_main(manifest_path: str, shard_id: int, date: str, out_dir: str = "batches/public-merged") -> None:
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    all_files = manifest["files"]
    files = shard_files(all_files, shard_id)
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%H%M%S")
    out_path = Path(out_dir) / date / f"shard-{shard_id}-{ts}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    accepted = 0
    skipped = 0
    dups = 0

    for rel in tqdm(files, desc=f"Shard {shard_id}"):
        url = f"{CDN_ROOT}/{date}/{rel}"
        ext = Path(rel).suffix.lower()

        try:
            if ext == ".jsonl":
                records = stream_jsonl(url)
            elif ext == ".parquet":
               
