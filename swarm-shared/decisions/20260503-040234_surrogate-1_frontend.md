# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix), and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed file manifest** (`manifest/<DATE_FOLDER>.json`) generated once per date folder to avoid recursive HF API calls and rate limits.
- Downloads only assigned shard files via **HF CDN direct URLs** (`resolve/main/...`) — no Authorization header, bypasses `/api/` 429 limits.
- Projects heterogeneous HF repo files to `{prompt, response}` at parse time (avoids `pyarrow.CastError` from mixed schemas).
- Deduplicates via central `lib/dedup.py` md5 store and writes deterministic shard outputs:
  ```
  batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
  ```
- Reuses existing GitHub Actions matrix setup; only the worker script and manifest generation step change.

---

### Steps (timeboxed)

1. **Create `bin/manifest-gen.py`** (15 min) — run once per date folder from Mac (or cron) after rate-limit window clears; does one `list_repo_tree` per date folder and saves `manifest/<DATE>.json`.
2. **Create `bin/dataset-enrich.py`** (60 min) — new worker:
   - Parse `SHARD_ID`, `SHARD_TOTAL`, `DATE_FOLDER`.
   - Load manifest JSON.
   - Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`.
   - For each assigned file:
     - Build CDN URL: `f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}"`.
     - Stream download with `requests.get(..., stream=True)`.
     - If parquet → `pyarrow.parquet.read_table(...).to_pylist()`; project `{prompt, response}`.
     - If JSONL/JSON → line-by-line parse and project.
     - Skip unknown extensions.
   - For each record, compute md5 of canonical `prompt+response`; call `lib/dedup.is_duplicate(md5)`; skip if seen.
   - Collect accepted records; write sorted by md5 to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
3. **Update `bin/dataset-enrich.sh`** (10 min) — thin wrapper that invokes `python3 bin/dataset-enrich.py` with env-vars for backward compat.
4. **Update workflow** (10 min) — ensure matrix still passes `SHARD_ID`/`SHARD_TOTAL`; optionally add step to fetch manifest artifact if not present.
5. **Test locally** (25 min) — run with small manifest subset; verify CDN fetch, projection, dedup, output format.

---

### Code snippets

#### `bin/manifest-gen.py`
```python
#!/usr/bin/env python3
"""
Generate manifest for a date folder to avoid recursive HF API during ingestion.
Usage:
  HF_TOKEN=... python3 bin/manifest-gen.py --date 2026-05-03
"""
import argparse
import json
import os
from pathlib import Path
from huggingface_hub import HfApi

API = HfApi()
REPO = "datasets/axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out-dir", default="manifest")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.date}.json"

    # Single non-recursive tree call per date folder
    tree = API.list_repo_tree(REPO, path=args.date, recursive=False)
    files = []
    for entry in tree:
        if entry.type == "file":
            files.append(entry.path)

    # Also include nested folders one level down (non-recursive per folder)
    nested = []
    for entry in tree:
        if entry.type == "dir":
            sub = API.list_repo_tree(REPO, path=entry.path, recursive=False)
            for se in sub:
                if se.type == "file":
                    nested.append(se.path)

    all_files = sorted(set(files + nested))
    manifest = {
        "date": args.date,
        "repo": REPO,
        "files": all_files,
    }

    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(all_files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

#### `lib/dedup.py` (unchanged contract)
```python
import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / "dedup.db"

def _ensure_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def is_duplicate(md5: str) -> bool:
    conn = _ensure_db()
    cur = conn.execute("SELECT 1 FROM seen WHERE md5 = ?", (md5,))
    exists = cur.fetchone() is not None
    if not exists:
        conn.execute("INSERT INTO seen (md5) VALUES (?)", (md5,))
        conn.commit()
    conn.close()
    return exists
```

#### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 public dataset ingestion.

Env:
  SHARD_ID (int, required)
  SHARD_TOTAL (int, default 16)
  DATE_FOLDER (str, default today YYYY-MM-DD)
  HF_TOKEN (optional for private repos; not used for CDN public files)
"""
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import requests

sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate

REPO_DATASET = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_DATASET}/resolve/main"

def _hash_slug(path: str) -> int:
    return int(hashlib.sha256(path.encode()).hexdigest(), 16)

def _project_record(raw: dict) -> dict | None:
    # Heuristic projection to {prompt, response}
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def _compute_md5(record: dict) -> str:
    canonical = f"{record['prompt']}\n---SEP---\n{record['response']}"
    return hashlib.md5(canonical.encode()).hexdigest()

def _download_cdn(url: str, out_path: Path) -> None:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def main() -> None:
    shard_id = int(os.environ["SHARD_ID"])
    shard_total = int(os.environ.get("SHARD_TOTAL", "16"))
    date_folder = os.environ.get("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    manifest_path = Path("manifest") / f"{date_folder}.json"
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files", [])
    assigned = [
        f for f in files
        if _hash_slug(f) % shard_total == shard
