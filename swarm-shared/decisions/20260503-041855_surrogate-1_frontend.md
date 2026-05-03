# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. Add `src/manifest.py` — single API call to `list_repo_tree` per date folder, save JSON manifest with CDN URLs.
2. Add `src/worker.py` — reads manifest, streams files via CDN (`resolve/main/...`), projects to `{prompt, response}`, dedups via central SQLite, outputs `shard-<N>-<ts>.jsonl`.
3. Update `bin/dataset-enrich.sh` → thin wrapper that invokes `python -m src.worker` with proper env and error handling.
4. Add `requirements-dev.txt` (optional) and update `requirements.txt` with `requests` if missing.
5. Ensure executable bits and Bash shebang in wrapper.

### Code Snippets

#### src/manifest.py
```python
#!/usr/bin/env python3
"""
Generate a manifest for a date folder:
  list_repo_tree(recursive=False) -> CDN URLs only.
Save as manifest-<date>.json for use by CDN-only workers.
"""
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

REPO = "datasets/axentx/surrogate-1-training-pairs"
OUT_DIR = "manifests"

def main(date_folder: str) -> None:
    api = HfApi()
    # One API call per date folder (non-recursive)
    items = api.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"manifest-{date_folder}.json")

    files = []
    for item in items:
        if item.type != "file":
            continue
        # CDN URL bypasses /api/ auth checks and rate limits
        cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{date_folder}/{item.path}"
        files.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
        })

    manifest = {
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": REPO,
        "files": files,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: manifest.py <date-folder>")
        sys.exit(1)
    main(sys.argv[1])
```

#### src/worker.py
```python
#!/usr/bin/env python3
"""
CDN-only worker:
- Reads manifest-<date>.json
- Streams each file via CDN URL (no HF API during load)
- Projects to {prompt, response}
- Dedups via central SQLite store (shared across runs)
- Outputs shard-<N>-<ts>.jsonl
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

DB_PATH = Path("dedup.db")
BATCH_SIZE = 500
TIMEOUT = 30
RETRIES = 3
BACKOFF = 5

def ensure_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("CREATE TABLE IF NOT EXISTS seen_md5 (md5 TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def is_duplicate(conn: sqlite3.Connection, md5: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_md5 WHERE md5 = ?", (md5,))
    return cur.fetchone() is not None

def mark_seen(conn: sqlite3.Connection, md5: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen_md5 (md5) VALUES (?)", (md5,))

def robust_get(url: str) -> Optional[bytes]:
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == RETRIES - 1:
                print(f"Failed {url}: {exc}", file=sys.stderr)
                return None
            time.sleep(BACKOFF * (attempt + 1))
    return None

def project_to_pair(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return {prompt, response} from heterogeneous schemas."""
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
    response = obj.get("response") or obj.get("output") or obj.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def hash_pair(pair: Dict[str, str]) -> str:
    raw = f"{pair['prompt']}\0{pair['response']}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def stream_file(url: str, conn: sqlite3.Connection):
    data = robust_get(url)
    if data is None:
        return []

    pairs = []
    try:
        # Try parquet first
        table = pq.read_table(pa.BufferReader(data))
        df = table.to_pydict()
        rows = [dict(zip(df, t)) for t in zip(*df.values())]
    except Exception:
        # Fallback: newline JSON
        rows = [json.loads(ln) for ln in data.decode("utf-8").splitlines() if ln.strip()]

    for row in rows:
        pair = project_to_pair(row)
        if pair is None:
            continue
        md5 = hash_pair(pair)
        if is_duplicate(conn, md5):
            continue
        mark_seen(conn, md5)
        pairs.append(pair)
    return pairs

def run_worker(manifest_path: Path, shard_id: int, out_dir: Path) -> None:
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    conn = ensure_db()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    out_path = out_dir / f"shard-{shard_id}-{ts}.jsonl"

    total = 0
    with out_path.open("w", encoding="utf-8") as out_f:
        for item in tqdm(manifest["files"], desc=f"Shard {shard_id}"):
            pairs = stream_file(item["cdn_url"], conn)
            for p in pairs:
                out_f.write(json.dumps(p, ensure_ascii=False) + "\n")
                total += 1
            # Periodic commit to reduce lock contention
            if total % BATCH_SIZE == 0:
                conn.commit()

    conn.commit()
    conn.close()
    print(f"Shard {shard_id}: wrote {total} pairs -> {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--shard", required=True, type=int)
    parser.add_argument("--out-dir", default="batches/public-merged")
    args = parser.parse_args()
    run_worker(args.manifest, args.shard, Path(args.out_dir))
```

#### bin/dataset-enrich.sh
```bash
#!/usr/bin/env bash
# Thin wrapper for GitHub Actions matrix shard.
# Usage: dataset-enrich.sh <date-folder> <shard-id>
set -euo pipefail

cd "$(dirname "$0")/.."

DATE_FOLDER="${1:-}"
SHARD_ID="${2:-}"
if [[ -z "$DATE_FOLDER" || -z "$SHARD_ID" ]]; then
  echo "Usage: $0 <date-folder> <shard-id>"
  exit 1
fi

# Ensure Python path and deps
export PYTHONPATH="${
