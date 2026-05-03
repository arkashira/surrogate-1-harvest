# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree(path, recursive=False)` per date folder → saves JSON manifest locally
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **HF CDN bypass** (`resolve/main/...`) — no API auth during streaming
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids PyArrow CastError)
- Dedup via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Returns exit code 0 on success, non-zero on hard failure (GitHub Actions will retry)

### Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (env):
  SHARD_ID=0..15
  SHARD_TOTAL=16
  DATE=2026-04-29
  HF_TOKEN=<write-token>
  REPO=datasets/axentx/surrogate-1-training-pairs
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download

# ---- config ----
REPO = os.getenv("REPO", "datasets/axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
API = HfApi(token=HF_TOKEN)

# ---- helpers ----
def deterministic_shard(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def cdn_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"

def load_dedup_store():
    # delegate to existing lib/dedup.py if present; simple fallback here
    db_path = Path(__file__).parent / "lib" / "dedup.db"
    return db_path

def is_duplicate(md5: str) -> bool:
    db = load_dedup_store()
    if not db.exists():
        return False
    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM hashes WHERE md5=? LIMIT 1", (md5,))
        return cur.fetchone() is not None

def mark_seen(md5: str):
    db = load_dedup_store()
    db.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS hashes (md5 TEXT PRIMARY KEY)")
        conn.execute("INSERT OR IGNORE INTO hashes (md5) VALUES (?)", (md5,))
        conn.commit()

# ---- schema projection ----
def project_to_pair(obj) -> dict | None:
    """
    Best-effort projection to {prompt, response}.
    Supports common keys seen in surrogate-1 training pairs.
    """
    if isinstance(obj, dict):
        # direct fields
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
        response = obj.get("response") or obj.get("output") or obj.get("answer")
        if prompt is not None and response is not None:
            return {"prompt": str(prompt), "response": str(response)}
        # nested
        for k in ("messages", "conversation", "turns"):
            if k in obj and isinstance(obj[k], list) and len(obj[k]) >= 2:
                msgs = obj[k]
                if len(msgs) == 2:
                    a, b = msgs
                    pa = a.get("content") or a.get("text")
                    pb = b.get("content") or b.get("text")
                    if pa and pb:
                        return {"prompt": str(pa), "response": str(pb)}
    return None

# ---- worker ----
def run():
    if not HF_TOKEN:
        print("HF_TOKEN required", file=sys.stderr)
        return 1

    folder = f"batches/public-raw/{DATE}"
    print(f"[{SHARD_ID}] listing {folder} ...")

    try:
        tree = API.list_repo_tree(
            repo_id=REPO,
            path=folder,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        print(f"[{SHARD_ID}] list_repo_tree failed: {exc}", file=sys.stderr)
        return 2

    files = [item.rfilename for item in tree if item.rfilename.endswith((".jsonl", ".parquet", ".json"))]
    if not files:
        print(f"[{SHARD_ID}] no files in {folder}")
        return 0

    manifest_path = Path("manifest.json")
    manifest_path.write_text(json.dumps({DATE: files}, separators=(",", ":")))
    print(f"[{SHARD_ID}] manifest saved ({len(files)} files)")

    my_files = [f for f in files if deterministic_shard(f) == SHARD_ID]
    print(f"[{SHARD_ID}] processing {len(my_files)} files")

    out_dir = Path("batches") / "public-merged" / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    processed = 0
    uploaded = 0
    skipped_dup = 0

    for fn in my_files:
        url = cdn_url(fn)
        try:
            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[{SHARD_ID}] download failed {fn}: {exc}", file=sys.stderr)
            continue

        if fn.endswith(".parquet"):
            # stream parquet via pyarrow without loading full dataset
            import pyarrow.parquet as pq
            import io
            try:
                buf = io.BytesIO(resp.content)
                table = pq.read_table(buf, columns=["prompt", "response"]
                                      if {"prompt", "response"}.issubset(pq.read_schema(buf).names)
                                      else None)
                rows = table.to_pylist()
            except Exception as exc:
                # fallback: project row-by-row if schema mismatch
                print(f"[{SHARD_ID}] parquet schema fallback {fn}: {exc}", file=sys.stderr)
                continue
        elif fn.endswith(".jsonl"):
            rows = []
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        elif fn.endswith(".json"):
            try:
                data = resp.json()
                rows = data if isinstance(data, list) else [data]
            except Exception as exc:
                print(f"[{SHARD_ID}] json parse failed {fn}: {exc}", file=sys.stderr)
                continue
        else:
            continue

        with out_path.open("a", encoding="utf-8") as f:
            for row in rows:
                pair = project_to_pair(row)
                if not pair:
                    continue
                md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
                if is_duplicate(md5):
                    skipped_dup += 1
                    continue
                f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                mark_seen(md5)
                uploaded += 1
        processed += 1

    print(f"[{SHARD_ID}] done: processed={processed} uploaded={uploaded} skipped_dup={skipped_dup}")

    # upload to HF dataset repo
    if out_path.exists() and out_path.stat().st_size > 0:
        remote_path = f"
