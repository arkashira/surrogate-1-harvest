# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (write)
- Single `list_repo_tree(path, recursive=False)` for `public-merged/<DATE>/` (or falls back to `training-pairs/` if needed) → saves list to `manifest.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, avoids HF API 429
- Streams JSONL/Parquet, projects to `{prompt, response}`, computes `md5` for dedup against central store (`lib/dedup.py`)
- Writes output to `batches/public-merged/<DATE>/shard<SHARD_ID>-<HHMMSS>.jsonl`
- Commits via HF Hub (one commit per shard) — respects 128/hr/repo cap by using deterministic shard → repo mapping if needed (future)
- Exits 0 on success, non-zero on hard failure (GitHub Actions will retry)

### Code snippets

`bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 training pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""
import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

REPO_DATASET = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{REPO_DATASET}/resolve/main"
DATE_FMT = "%Y-%m-%d"
BATCH_DIR = "batches/public-merged"

# Deterministic shard assignment
def shard_for(slug: str, total: int) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % total

def iso_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")

def list_date_files(date_str: str) -> list[str]:
    """Single API call to list files for a date folder."""
    folder = f"{BATCH_DIR}/{date_str}"
    try:
        tree = list_repo_tree(REPO_DATASET, folder=folder, repo_type="dataset")
    except Exception:
        # Fallback: list root training-pairs folder if date folder doesn't exist yet
        tree = list_repo_tree(REPO_DATASET, folder="training-pairs", repo_type="dataset")
    # Keep only files (not directories)
    return [item.rfilename for item in tree if item.type == "file"]

def cdn_download(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """Project raw record to {prompt, response}. Add more schema adapters as needed."""
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def md5_of_pair(pair: Dict[str, str]) -> str:
    blob = json.dumps(pair, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.md5(blob).hexdigest()

def main() -> int:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date_str = os.getenv("DATE", datetime.utcnow().strftime(DATE_FMT))
    hf_token = os.getenv("HF_TOKEN", "")

    if not hf_token:
        print("HF_TOKEN is required", file=sys.stderr)
        return 1

    api = HfApi(token=hf_token)
    out_dir = Path(f"{BATCH_DIR}/{date_str}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{shard_id}-{iso_ts()}.jsonl"

    # 1) List files once
    files = list_date_files(date_str)
    manifest_path = Path("manifest.json")
    manifest_path.write_text(json.dumps({"date": date_str, "files": files}, sort_keys=True))

    # 2) Central dedup store (SQLite)
    from lib.dedup import DedupStore
    dedup = DedupStore()

    processed = 0
    written = 0
    with out_path.open("w", encoding="utf-8") as fout:
        for rel_path in files:
            slug = rel_path.replace("/", "_").replace(".jsonl", "").replace(".parquet", "")
            if shard_for(slug, shard_total) != shard_id:
                continue

            # CDN bypass: no auth header
            url = f"{CDN_ROOT}/{rel_path}"
            try:
                raw_bytes = cdn_download(url)
            except Exception as exc:
                print(f"Failed to download {rel_path}: {exc}", file=sys.stderr)
                continue

            # Parse JSONL or Parquet
            records: list[Dict[str, Any]] = []
            if rel_path.endswith(".jsonl"):
                for line in raw_bytes.decode().splitlines():
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except Exception:
                            continue
            elif rel_path.endswith(".parquet"):
                import pyarrow.parquet as pq
                import io
                table = pq.read_table(io.BytesIO(raw_bytes))
                records = table.to_pylist()
            else:
                print(f"Unsupported file {rel_path}", file=sys.stderr)
                continue

            for rec in records:
                pair = project_to_pair(rec)
                if not pair["prompt"] or not pair["response"]:
                    continue
                pair_md5 = md5_of_pair(pair)
                processed += 1
                if dedup.exists(pair_md5):
                    continue
                dedup.add(pair_md5)
                fout.write(json.dumps(pair, ensure_ascii=False) + "\n")
                written += 1

    print(f"Shard {shard_id}: processed={processed} written={written} out={out_path}")

    # 3) Upload to HF dataset (single commit per shard)
    if out_path.stat().st_size > 0:
        try:
            api.upload_file(
                path_or_fileobj=str(out_path),
                path_in_repo=str(out_path.relative_to(".")),
                repo_id=REPO_DATASET,
                repo_type="dataset",
                commit_message=f"shard{shard_id} {date_str} {iso_ts()}",
            )
        except Exception as exc:
            print(f"Upload failed: {exc}", file=sys.stderr)
            return 1
    else:
        print("No new pairs to upload.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
```

`lib/dedup.py` (minimal central md5 store)
```python
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path("dedup.db")

class DedupStore:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS md5s (md5 TEXT PRIMARY KEY)")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def exists(self, md5: str) -> bool:
        with self._conn
