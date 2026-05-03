# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses `list_repo_tree(recursive=False)` per date folder → deterministic shard assignment via `hash(slug) % SHARD_TOTAL`
- Downloads only assigned files via **HF CDN bypass** (`resolve/main/...` URLs, no Authorization header)
- Projects to `{prompt, response}` at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central `lib/dedup.py` SQLite store
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Exits non-zero on fatal errors; zero on success (even if no files assigned)

### Steps (≤2h)
1. Create `bin/dataset-enrich.py` (manifest + CDN bypass + schema projection)
2. Add `lib/dedup.py` (central SQLite dedup module)
3. Update `.github/workflows/ingest.yml` to generate manifest once and pass to matrix jobs
4. Remove old `bin/dataset-enrich.sh`
5. Smoke-test locally with `HF_TOKEN`

---

## `lib/dedup.py`

```python
import os
import sqlite3
from pathlib import Path
from typing import Optional

def get_db() -> sqlite3.Connection:
    db_path = os.getenv("DEDUP_DB", str(Path(__file__).parent.parent / "dedup.sqlite"))
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_hashes ("
        "  md5 TEXT PRIMARY KEY,"
        "  ts DATETIME DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.commit()
    return conn

def is_duplicate(conn: sqlite3.Connection, md5_hex: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_hashes WHERE md5=?", (md5_hex,))
    return cur.fetchone() is not None

def mark_seen(conn: sqlite3.Connection, md5_hex: str) -> None:
    try:
        conn.execute("INSERT INTO seen_hashes (md5) VALUES (?)", (md5_hex,))
    except sqlite3.IntegrityError:
        pass  # race ok
```

---

## `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass, manifest-driven ingestion worker for surrogate-1.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --manifest ./manifest.json \
    --out-dir ./batches/public-merged
"""

import os
import sys
import json
import hashlib
import argparse
import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from tqdm import tqdm

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from dedup import get_db, is_duplicate, mark_seen

# ── constants ──────────────────────────────────────────────────────────────
HF_API = "https://huggingface.co/api"
HF_CDN = "https://huggingface.co/datasets"
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB

# ── helpers ────────────────────────────────────────────────────────────────
def slug_hash_bucket(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

def load_manifest(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)

def cdn_download(url: str, dst: Path) -> Path:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                f.write(chunk)
    return dst

def project_to_pair(file_path: Path) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous HF parquet/jsonl to {prompt, response}.
    Returns None if projection fails or fields missing.
    """
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".parquet":
            tbl = pq.read_table(file_path, columns=["prompt", "response"])
            df = tbl.to_pandas()
        elif suffix in (".jsonl", ".json"):
            df = pa.json.read_json(file_path).to_pandas()
        else:
            return None

        if "prompt" not in df.columns or "response" not in df.columns:
            return None

        row = df[["prompt", "response"]].dropna().iloc[0]
        return {"prompt": str(row["prompt"]), "response": str(row["response"])}
    except Exception:
        return None

# ── worker ─────────────────────────────────────────────────────────────────
def run_worker(
    repo: str,
    manifest: Dict[str, Any],
    shard_id: int,
    shard_total: int,
    date: str,
    hf_token: str,
    out_dir: Path,
) -> int:
    conn = get_db()
    date_files = manifest.get(date, [])
    assigned = []

    for entry in date_files:
        slug = entry.get("slug") or entry.get("path")
        if slug is None:
            continue
        if slug_hash_bucket(slug, shard_total) != shard_id:
            continue
        assigned.append(entry)

    if not assigned:
        print(f"[shard-{shard_id}] No files assigned for {date}.", file=sys.stderr)
        return 0

    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_file = out_dir / date / f"shard{shard_id}-{ts}.jsonl"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    seen_local = 0
    written = 0

    with open(out_file, "w", encoding="utf-8") as out_f:
        for entry in tqdm(assigned, desc=f"[shard-{shard_id}]"):
            slug = entry.get("slug") or entry.get("path")
            path = entry.get("path", slug)
            md5_hex = entry.get("md5") or hashlib.md5(slug.encode()).hexdigest()

            if is_duplicate(conn, md5_hex):
                seen_local += 1
                continue

            # CDN bypass: no auth header required for public datasets
            cdn_url = f"{HF_CDN}/{repo}/resolve/main/{path}"
            tmp = Path("tmp") / path.replace("/", "_")
            tmp.parent.mkdir(parents=True, exist_ok=True)

            try:
                cdn_download(cdn_url, tmp)
                pair = project_to_pair(tmp)
                if pair and pair["prompt"].strip() and pair["response"].strip():
                    out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1
                    mark_seen(conn, md5_hex)
            except Exception as exc:
                print(f"[shard-{shard_id}] Failed {path}: {exc}", file=sys.stderr)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

    print(f"[shard-{shard_id}] Done. Written={written}, Dupes(local)={seen_local}, Out={out_file}")
    return 0

# ── main ──────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="surrogate-1 CDN-bypass worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--manifest", type=Path, default=Path("manifest.json"))
    parser.add_argument("--out-dir", type=Path, default=Path("batches/public-merged"))
    args = parser.parse_args()

    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SH
