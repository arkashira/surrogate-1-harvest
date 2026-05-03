# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.sh`** → **`bin/dataset-enrich.py`**  
   - Single API call (Mac) to `list_repo_tree(path, recursive=False)` for one date folder → save `manifest.json`.  
   - Worker reads manifest, downloads each file via **CDN URL** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with **no Authorization header** → bypasses `/api/` rate limits entirely.  
   - Stream-parse each file, project to `{prompt, response}` only, compute `md5` for dedup, emit normalized JSONL.  
   - Deterministic shard assignment via `hash(slug) % 16 == SHARD_ID`.  
   - Upload output to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **`lib/dedup.py`** (unchanged)  
   - Keep central md5 dedup store; worker imports and uses it.

3. **`.github/workflows/ingest.yml`**  
   - Update matrix runner to invoke `python bin/dataset-enrich.py` instead of shell script.  
   - Pass `SHARD_ID`, `HF_TOKEN`, `DATE` (defaults to today) as env.

4. **`requirements.txt`**  
   - Add `requests` (for CDN downloads), keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`.

5. **Delete** old `bin/dataset-enrich.sh` after verifying Python worker parity.

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingest worker for surrogate-1-training-pairs.

Usage (GH Actions):
  SHARD_ID=0 python bin/dataset-enrich.py

Environment:
  SHARD_ID          0-15 (deterministic shard assignment)
  HF_TOKEN          HuggingFace write token
  DATE              YYYY-MM-DD (default: today)
  REPO              datasets repo (default: axentx/surrogate-1-training-pairs)
"""
import os
import sys
import json
import hashlib
import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, list_repo_tree

# ── config ──────────────────────────────────────────────────────────────────
REPO = os.getenv("REPO", "axentx/surrogate-1-training-pairs")
DATE = os.getenv("DATE", datetime.date.today().isoformat())
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"
OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── dedup store ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa: E402

dedup = DedupStore()

# ── helpers ─────────────────────────────────────────────────────────────────
def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str, shard: int, total: int = 16) -> bool:
    return slug_hash(slug) % total == shard

def safe_project(record: Dict[str, Any]) -> Dict[str, Any] | None:
    """Project heterogeneous schemas to {prompt, response}. Return None if invalid."""
    prompt = record.get("prompt") or record.get("input") or record.get("question")
    response = record.get("response") or record.get("output") or record.get("answer")
    if not prompt or not response:
        return None
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def download_file_cdn(path: str) -> bytes:
    url = f"{BASE_CDN}/{path}"
    # CDN bypass: no Authorization header
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

# ── main ────────────────────────────────────────────────────────────────────
def main() -> None:
    # 1) list top-level date folder once (cheap API call)
    try:
        tree = list_repo_tree(REPO, path=DATE, recursive=False)
    except Exception as e:
        print(f"Failed to list repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    files = [item.rfilename for item in tree if item.type == "file"]
    if not files:
        print(f"No files found for {DATE}", file=sys.stderr)
        sys.exit(0)

    # 2) build manifest (slug → path) and filter by shard
    manifest: List[Dict[str, str]] = []
    for f in files:
        slug = Path(f).stem
        if belongs_to_shard(slug, SHARD_ID):
            manifest.append({"slug": slug, "path": f})

    manifest_path = OUT_DIR / f"manifest-shard{SHARD_ID}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Shard {SHARD_ID}: {len(manifest)} files assigned")

    # 3) process each file via CDN, dedup, emit
    out_path = OUT_DIR / f"shard{SHARD_ID}-{datetime.datetime.utcnow().strftime('%H%M%S')}.jsonl"
    written = 0
    skipped = 0
    duped = 0

    with out_path.open("w", encoding="utf-8") as out_f:
        for entry in manifest:
            try:
                raw = download_file_cdn(entry["path"])
            except Exception as exc:
                print(f"Download failed {entry['path']}: {exc}", file=sys.stderr)
                continue

            # Try parquet first (common), fallback to jsonl
            import io
            try:
                import pyarrow.parquet as pq
                table = pq.read_table(io.BytesIO(raw))
                rows = table.to_pylist()
            except Exception:
                # assume jsonl
                rows = [json.loads(l) for l in raw.decode().strip().splitlines() if l.strip()]

            for row in rows:
                pair = safe_project(row)
                if not pair:
                    skipped += 1
                    continue

                digest = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
                if dedup.exists(digest):
                    duped += 1
                    continue

                dedup.add(digest)
                out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                written += 1

    print(f"Shard {SHARD_ID}: written={written} skipped={skipped} duped={duped} -> {out_path}")

    # 4) upload to HF dataset (single commit per shard/run)
    if written > 0:
        API.upload_file(
            path_or_fileobj=str(out_path),
            path_in_repo=out_path.relative_to(Path.cwd()).as_posix(),
            repo_id=REPO,
            repo_type="dataset",
            commit_message=f"shard{SHARD_ID} {DATE} public-merged",
        )
        print("Upload complete")

if __name__ == "__main__":
    main()
```

### `lib/dedup.py` (unchanged, imported above)

```python
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dedup.db"

class DedupStore:
    def __init__(self):
        self.db_path = DB_PATH
        self._init()

    def _init(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS md5 (digest TEXT PRIMARY KEY)")

    def exists(self, digest: str) ->
