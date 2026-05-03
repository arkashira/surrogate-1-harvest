# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — single-file worker that:
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` from matrix
   - Calls HF API **once** (after rate-limit window) to list a single date folder via `list_repo_tree(..., recursive=False)`
   - Saves file list to `manifest.json`
   - Streams only assigned shard’s files via **CDN direct download** (`resolve/main/...` with no auth)
   - Projects each file to `{prompt, response}` at parse time (avoids `load_dataset` mixed-schema CastError)
   - Dedups via existing `lib/dedup.py` md5 store
   - Writes `shard-<N>-<HHMMSS>.jsonl` to `batches/public-merged/<date>/`

2. **Update `bin/dataset-enrich.sh`** → thin wrapper that:
   - Exports `PYTHONUNBUFFERED=1`, `SHELL=/bin/bash`
   - Validates `HF_TOKEN`
   - Invokes `python3 bin/worker.py` with matrix args

3. **Update `.github/workflows/ingest.yml`** → ensure:
   - Matrix `strategy: { shard: [0..15] }`
   - Each job uses `ubuntu-latest` with 7 GB runner
   - Single `list_repo_tree` step is optional (worker can do it); if done in separate job, pass manifest as artifact

4. **Add `requirements.txt`** entries if missing:
   ```
   huggingface_hub>=0.22.0
   pyarrow>=14.0.0
   numpy>=1.24.0
   requests>=2.31.0
   ```

### Code Snippets

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 public-dataset ingestion.
Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python3 bin/worker.py
"""

import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import list_repo_tree, hf_hub_download

# -- config --
REPO_ID = "axentx/surrogate-1-training-pairs"
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

SHARD_ID = int(os.getenv("SHARD_ID", 0))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", 16))
DATE_STR = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUT_DIR = Path("batches/public-merged") / DATE_STR
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.now(timezone.utc).strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard-{SHARD_ID}-{TIMESTAMP}.jsonl"

# central dedup store (shared via volume on HF Space; here we use local fallback)
DEDUP_DB = Path("dedup_hashes.jsonl")

HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
CDN_ROOT = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

# -- dedup --
def already_seen(md5_hex: str) -> bool:
    if not DEDUP_DB.exists():
        return False
    with DEDUP_DB.open() as f:
        for line in f:
            if line.strip() == md5_hex:
                return True
    return False

def mark_seen(md5_hex: str) -> None:
    with DEDUP_DB.open("a") as f:
        f.write(md5_hex + "\n")

# -- projection --
def project_to_pair(obj) -> dict:
    """Return {prompt, response} from heterogeneous schema."""
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

# -- shard assignment --
def assign_shard(file_path: str) -> int:
    slug = file_path.encode("utf-8")
    return int(hashlib.md5(slug).hexdigest(), 16) % TOTAL_SHARDS

# -- main --
def run() -> None:
    # 1) list single date folder once (avoids recursive paginate on full repo)
    print(f"[shard-{SHARD_ID}] listing {REPO_ID}/batches/public-merged/{DATE_STR}/ ...")
    try:
        items = list_repo_tree(
            repo_id=REPO_ID,
            path=f"batches/public-merged/{DATE_STR}",
            recursive=False,
            token=HF_TOKEN,
        )
    except Exception as e:
        # folder may not exist yet; start with empty manifest
        print(f"[shard-{SHARD_ID}] folder not found or error: {e}; using empty list")
        items = []

    files = [p.rpath for p in items if p.type == "file" and p.rpath.endswith((".jsonl", ".parquet"))]
    manifest_path = Path("manifest.json")
    manifest_path.write_text(json.dumps({"date": DATE_STR, "files": files}, indent=2))
    print(f"[shard-{SHARD_ID}] {len(files)} files in manifest")

    # 2) process assigned shard via CDN (no auth, bypass API rate limits during training)
    written = 0
    skipped_dup = 0
    errors = 0

    for fp in sorted(files):
        if assign_shard(fp) != SHARD_ID:
            continue

        try:
            if fp.endswith(".parquet"):
                # download via CDN (no auth header) to avoid API limits
                url = f"{CDN_ROOT}/{fp}"
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                tbl = pq.read_table(pa.BufferReader(r.content))
                batch = project_to_pair(tbl.to_pydict())
                # parquet may contain multiple rows; iterate
                rows = [{"prompt": p, "response": r} for p, r in zip(batch["prompt"], batch["response"])]
            else:
                # jsonl via CDN
                url = f"{CDN_ROOT}/{fp}"
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                rows = []
                for line in r.text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        rows.append(project_to_pair(obj))
                    except Exception as exc:
                        errors += 1
                        continue

            for row in rows:
                payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
                md5 = hashlib.md5(payload.encode("utf-8")).hexdigest()
                if already_seen(md5):
                    skipped_dup += 1
                    continue
                mark_seen(md5)
                OUT_FILE.write_text(json.dumps(row) + "\n", append=True)
                written += 1

        except Exception as exc:
            print(f"[shard-{SHARD_ID}] error processing {fp}: {exc}")
            errors += 1
            continue

    print(f"[shard-{SHARD_ID}] done: written={written}, dup_skipped={skipped_dup}, errors={errors}")
    print(f"[shard-{SHARD_ID}] output: {OUT_FILE}")


if __name__ == "__main__":
    run()
```

#### `bin/dataset-enrich.sh` (updated)
```bash
#!/usr/bin/env bash
# Thin wrapper for surrogate-1 ingestion worker.
# Invoked by GitHub Actions matrix (SHARD_ID, TOTAL_SHARDS).

set -euo pipefail
export SHELL=/bin/bash
export PYTHONUNBUFFERED=1

cd "$(dirname "$0")/.."

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN is required"
  exit 1
fi

python3 bin/worker.py
```

#### `.github/workflows/ing
