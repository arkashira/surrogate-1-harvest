# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed manifest** (`batches/public-merged/<DATE_FOLDER>/manifest.json`) produced by a lightweight Mac-side script (or prior workflow step) that calls `list_repo_tree` once per date folder and saves `{path, size, sha}` entries.
- Each shard deterministically hashes `path` → `idx % SHARD_TOTAL` and keeps only its slice.
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>`) with no Authorization header and streaming + chunked writes to avoid OOM.
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas).
- Deduplicates via central md5 store (`lib/dedup.py`) and emits `batches/public-merged/<DATE_FOLDER>/shard<SHARD_ID>-<HHMMSS>.jsonl`.
- Commits via HF API with deterministic filenames to avoid collisions; spreads writes across sibling repos if needed (hash-slug → repo mapping).
- Reuses running Lightning Studio for any local orchestration/testing; never runs `model.from_pretrained()` on Mac.

---

## Code snippets

### 1) Manifest generator (run on Mac / prior step)

```bash
# bin/gen-manifest.sh
#!/usr/bin/env bash
set -euo pipefail
DATE_FOLDER="${1:-$(date +%Y-%m-%d)}"
OUT="batches/public-merged/${DATE_FOLDER}/manifest.json"
mkdir -p "$(dirname "$OUT")"

python3 - <<PY
import os, json, sys
from huggingface_hub import list_repo_tree

REPO = "axentx/surrogate-1-training-pairs"
folder = f"batches/public-merged/{sys.argv[1]}"
items = list_repo_tree(REPO, path=folder, recursive=False)
files = [{"path": f.path, "size": f.size, "sha": f.sha} for f in items if f.type == "file"]
os.makedirs(os.path.dirname("$OUT"), exist_ok=True)
with open("$OUT", "w") as f:
    json.dump(files, f)
print(f"Wrote {len(files)} entries to $OUT")
PY
```

Make executable:

```bash
chmod +x bin/gen-manifest.sh
```

---

### 2) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1.
Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py [DATE_FOLDER]
"""
import os
import sys
import json
import hashlib
import datetime
import requests
from pathlib import Path

HF_DATASET = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"

def parse_record(raw_bytes: bytes):
    """
    Project arbitrary file bytes to {prompt, response}.
    Extend per known schema; keep minimal to avoid pyarrow/mixed-schema issues.
    """
    # Example: assume JSONL lines with fields that may vary
    try:
        obj = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None

    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
    response = obj.get("response") or obj.get("output") or obj.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def deterministic_shard(path: str, total: int) -> int:
    h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    return h % total

def main():
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date_folder = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()

    manifest_path = Path(f"batches/public-merged/{date_folder}/manifest.json")
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path) as f:
        entries = json.load(f)

    my_paths = [
        e["path"] for e in entries
        if deterministic_shard(e["path"], shard_total) == shard_id
    ]
    print(f"Shard {shard_id}/{shard_total} → {len(my_paths)} files")

    # central dedup store (shared via lib/dedup.py)
    from lib.dedup import DedupStore
    dedup = DedupStore()

    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = Path(f"batches/public-merged/{date_folder}/shard{shard_id}-{ts}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped_dup = 0
    failed = 0

    with out_path.open("w", buffering=1 << 20) as out_f:
        for p in my_paths:
            url = f"{BASE_CDN}/{p}"
            try:
                # stream download; no auth header -> CDN bypass
                resp = requests.get(url, stream=True, timeout=60)
                resp.raise_for_status()
                for chunk in resp.iter_lines(chunk_size=8192):
                    if not chunk:
                        continue
                    rec = parse_record(chunk)
                    if rec is None:
                        continue
                    # content-based md5 dedup
                    md5 = hashlib.md5(json.dumps(rec, sort_keys=True).encode()).hexdigest()
                    if dedup.seen(md5):
                        skipped_dup += 1
                        continue
                    dedup.add(md5)
                    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as exc:
                failed += 1
                print(f"Error on {p}: {exc}", file=sys.stderr)

    print(f"Shard {shard_id} done: written={written}, dup={skipped_dup}, failed={failed}")
    print(f"Output: {out_path}")

if __name__ == "__main__":
    main()
```

---

### 3) Minimal `lib/dedup.py` (central md5 store)

```python
import sqlite3
import os
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(os.getenv("DEDUP_DB", "dedup_store.db"))

@contextmanager
def _conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_md5 (md5 TEXT PRIMARY KEY, ts REAL DEFAULT (julianday('now')))"
    )
    try:
        yield conn
    finally:
        conn.close()

class DedupStore:
    def seen(self, md5: str) -> bool:
        with _conn() as conn:
            cur = conn.execute("SELECT 1 FROM seen_md5 WHERE md5=?", (md5,))
            return cur.fetchone() is not None

    def add(self, md5: str) -> None:
        with _conn() as conn:
            conn.execute("INSERT OR IGNORE INTO seen_md5 (md5) VALUES (?)", (md5,))
            conn.commit()
```

---

### 4) Update workflow to use Python worker

```yaml
# .github/workflows/ingest.yml
name: Ingest (CDN-bypass)

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      date_folder:
        description: "Date folder (YYYY-MM-DD)"
        required: false
        default: ""

jobs:
  ingest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,
