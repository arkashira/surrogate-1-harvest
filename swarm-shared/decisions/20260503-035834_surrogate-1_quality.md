# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single API call** from the runner (once per workflow) to list one date folder via `list_repo_tree(path, recursive=False)` → saves `file-list.json` as an artifact.
- Each shard worker loads the manifest, keeps only its deterministic slice (`hash(slug) % SHARD_TOTAL == SHARD_ID`).
- Downloads assigned files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header → avoids 429 during data load.
- Projects each file to `{prompt, response}` at parse time (no schema assumptions), computes content md5 for dedup against the central SQLite store, and streams output as newline JSON.
- Writes to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with deterministic filename to prevent cross-shard collisions.
- Exits non-zero on fatal errors; logs summary counts (processed, skipped, uploaded).

### Code: `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py [DATE_FOLDER]
"""
import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import list_repo_tree

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = (sys.argv[1] if len(sys.argv) > 1 else datetime.utcnow().strftime("%Y-%m-%d"))

OUT_DIR = Path("batches/public-merged") / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# Central dedup SQLite store (must be shared/mounted or handled by HF Space in prod).
DEDUP_DB = Path("dedup.sqlite3")

def _init_db():
    import sqlite3
    conn = sqlite3.connect(str(DEDUP_DB))
    conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def _is_duplicate(conn, md5_hex):
    cur = conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5_hex,))
    return cur.fetchone() is not None

def _mark_seen(conn, md5_hex):
    try:
        conn.execute("INSERT INTO seen (md5) VALUES (?)", (md5_hex,))
    except sqlite3.IntegrityError:
        pass

def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def list_date_files():
    """Single API call to list files in DATE_FOLDER (non-recursive)."""
    items = list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        token=HF_TOKEN,
    )
    files = [it for it in items if it.type == "file"]
    return files

def cdn_url(path_in_repo: str) -> str:
    return f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path_in_repo}"

def project_to_pair(raw_bytes, ext):
    """
    Best-effort projection to {prompt,response}.
    Extend per known schema as needed.
    """
    ext = ext.lower()
    if ext == ".jsonl":
        for line in raw_bytes.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            # Common patterns
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt is not None and response is not None:
                yield {"prompt": str(prompt), "response": str(response)}
    elif ext == ".json":
        obj = json.loads(raw_bytes)
        if isinstance(obj, list):
            for item in obj:
                prompt = item.get("prompt") or item.get("input")
                response = item.get("response") or item.get("output")
                if prompt is not None and response is not None:
                    yield {"prompt": str(prompt), "response": str(response)}
        else:
            prompt = obj.get("prompt") or obj.get("input")
            response = obj.get("response") or obj.get("output")
            if prompt is not None and response is not None:
                yield {"prompt": str(prompt), "response": str(response)}
    elif ext == ".parquet":
        import pyarrow.parquet as pq
        import io
        table = pq.read_table(io.BytesIO(raw_bytes))
        df = table.to_pandas()
        for _, row in df.iterrows():
            prompt = row.get("prompt") or row.get("input")
            response = row.get("response") or row.get("output")
            if prompt is not None and response is not None:
                yield {"prompt": str(prompt), "response": str(response)}
    else:
        # Fallback: try to parse as simple text Q/A blocks if needed.
        pass

def main():
    if not HF_TOKEN:
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} | Date {DATE_FOLDER}")

    files = list_date_files()
    assigned = []
    for f in files:
        # Deterministic shard assignment by slug (filename without extension).
        slug = Path(f.path).stem
        if slug_hash(slug) % SHARD_TOTAL == SHARD_ID:
            assigned.append(f)

    print(f"Assigned {len(assigned)} files out of {len(files)}")

    conn = _init_db()
    written = 0
    skipped_dup = 0
    errors = 0

    with OUT_FILE.open("w", encoding="utf-8") as out_f:
        for f in assigned:
            try:
                # CDN bypass: direct resolve URL (no auth header).
                url = cdn_url(f.path)
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                raw = resp.content

                ext = Path(f.path).suffix
                for pair in project_to_pair(raw, ext):
                    content = f"{pair['prompt']}\n{pair['response']}"
                    md5 = hashlib.md5(content.encode()).hexdigest()
                    if _is_duplicate(conn, md5):
                        skipped_dup += 1
                        continue
                    _mark_seen(conn, md5)
                    out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1

            except Exception as exc:
                errors += 1
                print(f"Error processing {f.path}: {exc}", file=sys.stderr)

            # Periodic commit to avoid long transactions.
            conn.commit()

    conn.commit()
    conn.close()

    print(f"Done. Written={written} SkippedDup={skipped_dup} Errors={errors}")
    print(f"Output: {OUT_FILE}")

    if errors > 0 and written == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### Workflow changes (`.github/workflows/ingest.yml`)
- Add a step before the matrix to produce `file-list.json` artifact (single API call) and pass it to matrix jobs, or keep the per-shard `list_date_files()` call (cheap: one lightweight API call per shard). Prefer single call + artifact to minimize rate-limit pressure.

### Requirements (`requirements.txt` additions)
```
datasets
huggingface_hub
pyarrow
numpy
requests
```
