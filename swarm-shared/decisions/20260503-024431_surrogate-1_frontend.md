# surrogate-1 / frontend

### Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a single, production-grade Python worker:  
`bin/dataset-enrich.py`.

**Core invariants (non-negotiable):**
- Manifest-driven via **one** `list_repo_tree` per date folder (no recursive walks).  
- CDN-bypass downloads via `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/...` (no auth, avoids 429).  
- Deterministic shard assignment: `hash(rel_path) % SHARD_TOTAL`.  
- Stream-process Parquet → keep only `{prompt, response}`.  
- Content-level dedup: `md5(content)` checked against `lib/dedup.py` SQLite (cross-run) + transient in-memory set (intra-run).  
- Output: `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`, committed once per shard.  
- Fallback: if CDN fetch fails (403/404/timeout), retry once via authenticated `hf_hub_download`.

---

### 1. Script: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
dataset-enrich.py
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Env:
  SHARD_ID        (int, required)
  SHARD_TOTAL=16  (int, default 16)
  DATE            (YYYY-MM-DD, required)
  HF_TOKEN        (required for repo ops + fallback)
"""
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import pyarrow.parquet as pq
import requests
from huggingface_hub import Repository, hf_hub_download

# ---- config ----
REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_DIR = Path(__file__).parent.parent
LOCAL_REPO_DIR = BASE_DIR
BATCHES_PREFIX = f"batches/public-merged/{os.environ['DATE']}"
CDN_TEMPLATE = "https://huggingface.co/datasets/" + REPO_ID + "/resolve/main/{path}"

SHARD_ID = int(os.environ["SHARD_ID"])
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", 16))
HF_TOKEN = os.environ["HF_TOKEN"]
DATE = os.environ["DATE"]

# ---- logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ---- dedup store ----
DEDB_PATH = BASE_DIR / "lib" / "dedup.sqlite3"
DEDB_PATH.parent.mkdir(parents=True, exist_ok=True)


def init_dedup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DEDB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_md5 (md5 TEXT PRIMARY KEY, ts INTEGER)"
    )
    conn.commit()
    return conn


def already_seen(conn: sqlite3.Connection, md5: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_md5 WHERE md5 = ?", (md5,))
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, md5: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_md5 (md5, ts) VALUES (?, ?)",
        (md5, int(time.time())),
    )


# ---- repo + manifest ----
def get_repo() -> Repository:
    return Repository(
        local_dir=str(LOCAL_REPO_DIR),
        repo_id=REPO_ID,
        repo_type="dataset",
        token=HF_TOKEN,
    )


def list_parquet_files(repo: Repository) -> Iterable[str]:
    entries = repo.list_repo_tree(path=BATCHES_PREFIX, recursive=False)
    for e in entries:
        if e.path.endswith(".parquet"):
            yield e.path


def deterministic_shard(path: str) -> int:
    return hash(path) % SHARD_TOTAL


# ---- download ----
def cdn_fetch(path: str, timeout: int = 30) -> bytes:
    url = CDN_TEMPLATE.format(path=path)
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def fallback_fetch(path: str) -> bytes:
    local_path = hf_hub_download(
        repo_id=REPO_ID,
        filename=path,
        repo_type="dataset",
        token=HF_TOKEN,
    )
    return Path(local_path).read_bytes()


def fetch_file(path: str) -> bytes:
    try:
        return cdn_fetch(path)
    except Exception as e:
        logging.warning("CDN fetch failed for %s: %s; falling back", path, e)
        return fallback_fetch(path)


# ---- processing ----
def extract_pairs(data: bytes) -> Iterable[Tuple[str, str]]:
    with pq.ParquetFile(pq.ParquetFile(pq.BufferReader(data))) as pf:
        for batch in pf.iter_batches(columns=["prompt", "response"]):
            df = batch.to_pandas()
            for _, row in df.iterrows():
                prompt = str(row.get("prompt") or "").strip()
                response = str(row.get("response") or "").strip()
                if prompt and response:
                    yield prompt, response


def normalize_and_dedup(
    pairs: Iterable[Tuple[str, str]],
    dedup_conn: sqlite3.Connection,
    seen_local: Dict[str, bool],
) -> Iterable[Dict[str, str]]:
    for prompt, response in pairs:
        content = (prompt + "\n" + response).strip()
        md5 = hashlib.md5(content.encode("utf-8")).hexdigest()
        if md5 in seen_local or already_seen(dedup_conn, md5):
            continue
        seen_local[md5] = True
        mark_seen(dedup_conn, md5)
        yield {"prompt": prompt, "response": response}


# ---- output ----
def output_path(ts: str) -> Path:
    out_dir = LOCAL_REPO_DIR / BATCHES_PREFIX
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"shard{SHARD_ID}-{ts}.jsonl"


def commit_and_push(repo: Repository, out_file: Path) -> None:
    repo.git_add(str(out_file.relative_to(LOCAL_REPO_DIR)))
    repo.commit(f"Add enriched shard {SHARD_ID} for {DATE}")
    repo.push_to_hub()


# ---- main ----
def main() -> None:
    ts = datetime.utcnow().strftime("%H%M%S")
    repo = get_repo()
    dedup_conn = init_dedup_db()
    seen_local: Dict[str, bool] = {}

    out_file = output_path(ts)
    written = 0

    with out_file.open("w", encoding="utf-8") as f:
        for parquet_path in list_parquet_files(repo):
            if deterministic_shard(parquet_path) != SHARD_ID:
                continue

            logging.info("Processing %s", parquet_path)
            try:
                raw = fetch_file(parquet_path)
            except Exception as e:
                logging.error("Failed to fetch %s: %s", parquet_path, e)
                continue

            pairs = extract_pairs(raw)
            for record in normalize_and_dedup(pairs, dedup_conn, seen_local):
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1

    dedup_conn.commit()
    dedup_conn.close()

    if written == 0:
        logging.warning("No records written; removing empty file.")
        out_file.unlink(missing_ok=True)
        return

    logging.info("Wrote %d records to %s", written, out_file)
    commit_and_push(repo, out_file)
    logging.info("Done.")


if __name__ == "__main__":
    main()
```

---

### 2. Workflow: `.github/workflows/ingest.yml`

```yaml
name: Ingest

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
     
