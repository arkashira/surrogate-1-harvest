# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`.

### Changes

1. **Add `bin/worker.py`** — deterministic shard worker that:
   - Reads a pre-computed `manifest.json` (date → file list) produced once per cron by a lightweight Mac-side script (or cached from previous run).
   - **Filters its 1/16 slice by `hash(slug) % 16 == SHARD_ID`** (deterministic, stable across reruns) instead of brittle index slicing.
   - Downloads only assigned files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API calls during data load, bypassing 429 limits.
   - Projects each file to `{prompt, response}` at parse time (avoids `pyarrow.CastError` from heterogeneous schemas).
   - Computes per-row md5, checks central dedup store (`lib/dedup.py`), keeps non-duplicates.
   - Writes `shard-<N>-<HHMMSS>.jsonl` to `batches/public-merged/<date>/`.

2. **Add `bin/gen-manifest.py`** — run once per cron (or cached) on Mac/lightweight runner:
   - Uses HF API **once** per date folder with `list_repo_tree(path, recursive=False)` to avoid recursive pagination.
   - Emits `manifest.json` mapping `date -> [file_paths...]`.
   - Commits or uploads as artifact for workers to consume.

3. **Update `bin/dataset-enrich.sh`** → thin wrapper that:
   - Accepts `SHARD_ID` and `MANIFEST_URL` (or local path).
   - Invokes `python bin/worker.py --shard $SHARD_ID --manifest manifest.json --out-dir .`.
   - Keeps existing filename convention for compatibility.

4. **Update `.github/workflows/ingest.yml`** — add a one-off job (or step) to generate `manifest.json` as an artifact, then pass it to the 16-shard matrix. Workers fetch the artifact and run CDN-only.

5. **Update `lib/dedup.py`** — ensure it supports concurrent append/check from multiple workers safely (SQLite with `PRAGMA journal_mode=WAL` and short retries).

---

## Code Snippets

### `bin/gen-manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest.json for a date folder to avoid recursive HF API calls.
Run once per cron (Mac/lightweight) and upload as artifact.
"""
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import list_repo_tree

REPO = "axentx/surrogate-1-training-pairs"
DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUT = "manifest.json"

def main() -> None:
    # Expect date override via env or arg
    date = sys.argv[1] if len(sys.argv) > 1 else DATE
    prefix = f"{date}/"

    entries = list_repo_tree(REPO, path=prefix, recursive=False)
    files = [e.r_path for e in entries if e.type == "file"]

    manifest = {
        "date": date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {OUT}")

if __name__ == "__main__":
    main()
```

---

### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass worker for deterministic shard processing.
Usage:
  python bin/worker.py --shard 3 --manifest manifest.json --out-dir ./batches
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

from lib.dedup import DedupStore

HF_DATASETS = "https://huggingface.co/datasets"
REPO = "axentx/surrogate-1-training-pairs"

def cdn_url(file_path: str) -> str:
    return f"{HF_DATASETS}/{REPO}/resolve/main/{file_path}"

def project_to_pair(row: Dict[str, Any]) -> Dict[str, str] | None:
    """
    Project heterogeneous schema row to {prompt, response}.
    Returns None if required fields missing.
    """
    prompt = row.get("prompt") or row.get("input") or row.get("question")
    response = row.get("response") or row.get("output") or row.get("answer")
    if not prompt or not response:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def row_md5(pair: Dict[str, str]) -> str:
    return hashlib.md5(f"{pair['prompt']}\0{pair['response']}".encode()).hexdigest()

def shard_id_for(file_path: str, shard_count: int) -> int:
    """Deterministic shard assignment by file slug."""
    slug = Path(file_path).stem
    return hash(slug) % shard_count

def process_shard(
    shard_id: int,
    shard_count: int,
    manifest_path: Path,
    out_dir: Path,
    dedup: DedupStore,
) -> None:
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    files = [f for f in manifest["files"] if shard_id_for(f, shard_count) == shard_id]

    date = manifest.get("date", "unknown")
    out_dir = out_dir / "public-merged" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    out_path = out_dir / f"shard{shard_id}-{ts}.jsonl"

    accepted = 0
    skipped = 0
    duped = 0

    for file_path in tqdm(files, desc=f"Shard {shard_id}"):
        url = cdn_url(file_path)
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
            continue

        try:
            table = pq.read_table(pa.BufferReader(resp.content))
        except Exception as exc:
            print(f"Failed to decode parquet {file_path}: {exc}", file=sys.stderr)
            continue

        for batch in table.to_batches():
            cols = batch.column_names
            for i in range(batch.num_rows):
                row = {c: batch[c][i].as_py() for c in cols}
                pair = project_to_pair(row)
                if not pair:
                    skipped += 1
                    continue

                md5 = row_md5(pair)
                if dedup.exists(md5):
                    duped += 1
                    continue

                accepted += 1
                dedup.add(md5)
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(pair, ensure_ascii=False) + "\n")

    print(
        f"Shard {shard_id} done: accepted={accepted}, duped={duped}, skipped={skipped}, out={out_path}"
    )

def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-bypass shard worker")
    parser.add_argument("--shard", type=int, required=True, help="Shard index (0..N-1)")
    parser.add_argument(
        "--shard-count", type=int, default=16, help="Total shards (default 16)"
    )
    parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    parser.add_argument("--out-dir", default="./batches", help="Output root")
    parser.add_argument(
        "--dedup-db", default="./dedup.sqlite", help="Path to dedup SQLite DB"
    )
    args = parser.parse_args()

    dedup = DedupStore(args.dedup_db)
    process_shard
