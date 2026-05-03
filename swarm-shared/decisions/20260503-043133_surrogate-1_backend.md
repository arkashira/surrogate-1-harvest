# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — single-file, manifest-first worker:
   - Accepts `SHARD_ID`, `TOTAL_SHARDS`, `DATE` (YYYY-MM-DD) via env (with CLI fallback).
   - Uses one HF API call (`list_repo_tree`) for the target date folder → saves `manifest.json`.
   - Downloads files via **CDN bypass** (`resolve/main/...`) with no auth header.
   - Projects each file to `{prompt, response}` at parse time (avoids `load_dataset` mixed-schema CastError).
   - Deduplicates via `lib/dedup.py` (central md5 store).
   - Produces deterministic shard output: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **Add `bin/gen-manifest.py`** — run once per date folder from Mac:
   - Calls HF API **once** with `list_repo_tree(path, recursive=False)` for the target date folder.
   - Saves `manifest.json` to repo root (committed or passed to Actions) so workers never call `list_repo_files` recursively (avoids 429).

3. **Update `.github/workflows/ingest.yml`**:
   - Matrix `shard_id: [0..15]`.
   - Each job runs `python bin/worker.py --shard $SHARD_ID --manifest manifest.json`.
   - No recursive HF API calls during ingestion; only CDN fetches.

4. **Update `lib/dedup.py`** to be import-safe and accept `hash_str` without side effects on missing DB.

5. **Remove/Deprecate** heavy `dataset-enrich.sh` streaming logic that uses `load_dataset(streaming=True)` on heterogeneous repos.

---

### Code Snippets

#### `bin/gen-manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest.json for a date folder to avoid recursive HF API calls.
Run from Mac once per date folder after rate-limit window clears.
"""
import json
import os
import sys
from huggingface_hub import HfApi

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-04-29"
OUT = "manifest.json"

api = HfApi()
# Single non-recursive call per folder
tree = api.list_repo_tree(repo_id=HF_REPO, path=DATE, recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith((".parquet", ".jsonl"))]

manifest = {"date": DATE, "files": sorted(files)}
with open(OUT, "w") as f:
    json.dump(manifest, f, indent=2)
print(f"Wrote {len(files)} files to {OUT}")
```

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass worker for a deterministic shard.
Usage: python bin/worker.py --shard 3 --manifest manifest.json
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate, mark_seen  # noqa: E402

HF_REPO = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

def download_cdn(path: str, out_path: Path) -> Path:
    url = f"{CDN_ROOT}/{path}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        return out_path
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return out_path

def project_to_pair(batch_bytes: bytes):
    """Project raw parquet/jsonl bytes to {prompt,response} pairs; ignore extra cols."""
    tmp = Path("tmp") / f"{hashlib.sha256(batch_bytes).hexdigest()[:12]}.parquet"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_bytes(batch_bytes)
    try:
        table = pq.read_table(tmp, columns=["prompt", "response"])
    except Exception:
        # fallback: try common aliases
        try:
            table = pq.read_table(tmp)
            cols = set(table.column_names)
            prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
            response_col = next((c for c in ("response", "output", "completion") if c in cols), None)
            if not prompt_col or not response_col:
                return []
            table = table.select([prompt_col, response_col]).rename_columns(["prompt", "response"])
        except Exception:
            return []
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    df = table.to_pandas()
    pairs = []
    for _, row in df.iterrows():
        p = str(row.get("prompt") or "").strip()
        r = str(row.get("response") or "").strip()
        if p and r:
            pairs.append({"prompt": p, "response": r})
    return pairs

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--manifest", default="manifest.json")
    args = parser.parse_args()

    with open(args.manifest) as f:
        manifest = json.load(f)

    date = manifest["date"]
    all_files = sorted(manifest["files"])
    # deterministic shard split: hash slug -> bucket
    shard_files = [
        f for f in all_files
        if (int(hashlib.md5(f.encode()).hexdigest(), 16) % 16) == args.shard
    ]

    os.makedirs("batches/public-merged", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = Path(f"batches/public-merged/{date}/shard{args.shard}-{ts}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    for rel in tqdm(shard_files, desc=f"Shard {args.shard}"):
        local = Path("dl") / rel
        try:
            local = download_cdn(rel, local)
            raw = local.read_bytes()
            pairs = project_to_pair(raw)
        except Exception as e:
            print(f"Skip {rel}: {e}", file=sys.stderr)
            continue

        for pair in pairs:
            text = json.dumps(pair, ensure_ascii=False)
            h = hashlib.md5(text.encode()).hexdigest()
            if is_duplicate(h):
                continue
            mark_seen(h)
            with open(out_path, "a") as f:
                f.write(text + "\n")
            written += 1

    print(f"Shard {args.shard}: wrote {written} pairs to {out_path}")

if __name__ == "__main__":
    main()
```

#### `lib/dedup.py` (minimal, import-safe)
```python
import sqlite3
from pathlib import Path

DB_PATH = Path("dedup.db")

def _get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (hash TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def is_duplicate(hash_str: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("SELECT 1 FROM seen WHERE hash = ?", (hash_str,))
    return cur.fetchone() is not None

def mark_seen(hash_str: str):
    conn = _get_conn()
    conn.execute("INSERT OR IGNORE INTO seen (hash) VALUES (?)", (hash_str,))
    conn.commit()
```
